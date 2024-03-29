import torch as th
from torch import nn
from torch.optim import Adam

import numpy as np

from common.Agent import Agent
from common.Model import MeanValueNet, ActorNet
from common.utils import to_tensor, entropy, index_to_one_hot, index_to_one_hot_tensor, onehot_from_logits


class MFAC(Agent):

    def __init__(self, env, state_dim, action_dim, device='cpu',
                 memory_capacity=10000, max_steps=10000,
                 reward_gamma=0.99, reward_scale=1., done_penalty=None,
                 actor_hidden_size=64, critic_hidden_size=64,
                 actor_output_act=None, critic_output_act=None,
                 critic_loss="mse", actor_lr=0.01, critic_lr=0.01,
                 optimizer_type="adam", entropy_reg=0.01,
                 max_grad_norm=0.5, batch_size=1000, episodes_before_train=100,
                 epsilon_start=0.9, epsilon_end=0.01, epsilon_decay=200,
                 target_tau=0.01, target_update_step=10, max_episodes=1000):
        super().__init__(env, state_dim, action_dim, device, memory_capacity, max_steps, reward_gamma, reward_scale,
                         done_penalty, actor_hidden_size, critic_hidden_size, actor_output_act, critic_output_act,
                         critic_loss, actor_lr, critic_lr, optimizer_type, entropy_reg, max_grad_norm, batch_size,
                         episodes_before_train, epsilon_start, epsilon_end, epsilon_decay, target_tau,
                         target_update_step, max_episodes)

        self.actor = [
            ActorNet(self.state_dim, self.actor_hidden_size,
                     self.action_dim, self.actor_output_act).to(device)
            for _ in range(self.n_agents)]
        self.actor_target = [
            ActorNet(self.state_dim, self.actor_hidden_size,
                     self.action_dim, self.actor_output_act).to(device)
            for _ in range(self.n_agents)]
        self.actor_optimizer = [Adam(self.actor[i].parameters(), lr=self.actor_lr)
                                for i in range(self.n_agents)]
        self.critic = [
            MeanValueNet(self.global_state_dim, self.action_dim, self.critic_hidden_size, 1,
                         self.critic_output_act).to(device)
            for i in range(self.n_agents)]
        self.critic_target = [
            MeanValueNet(self.global_state_dim, self.action_dim, self.critic_hidden_size, 1,
                         self.critic_output_act).to(device)
            for i in range(self.n_agents)]
        for i in range(self.n_agents):
            self.critic_target[i].load_state_dict(self.critic[i].state_dict())
            self.actor_target[i].load_state_dict(self.actor[i].state_dict())
        self.critic_optimizer = [Adam(self.critic[i].parameters(), lr=self.critic_lr)
                                 for i in range(self.n_agents)]

    # agent interact with the environment to collect experience
    def interact(self):
        super()._take_one_step(use_mean=True)

    # train on a sample batch
    def train(self):
        if self.n_episodes < self.episodes_before_train:
            pass

        batch = self.memory.sample(self.batch_size)
        states_tensor = to_tensor(batch.states, self.device).view(-1, self.n_agents, self.state_dim)
        actions_index_tensor = to_tensor(batch.actions, self.device, 'int').view(-1, self.n_agents)
        rewards_tensor = to_tensor(batch.rewards, self.device).view(-1, self.n_agents)
        next_states_tensor = to_tensor(batch.next_states, self.device).view(-1, self.n_agents, self.state_dim)
        dones_tensor = to_tensor(batch.dones, self.device).view(-1, self.n_agents)
        mean_actions_tensor = to_tensor(batch.mean_actions, self.device).view(-1, self.n_agents, self.env.n_actions)
        global_state_tensor = to_tensor(batch.global_states, self.device).view(-1, self.n_agents, self.global_state_dim)
        next_global_state_tensor = to_tensor(batch.next_global_states, self.device).view(-1, self.n_agents, self.global_state_dim)

        # partial obs of each agent
        obs = [states_tensor[:, i] for i in range(self.n_agents)]
        next_obs = [next_states_tensor.view(-1, self.n_agents, self.state_dim)[:, i] for i in range(self.n_agents)]
        # get joint action
        next_act = [onehot_from_logits(pi(_next_obs)) for pi, _next_obs in zip(self.actor_target, next_obs)]

        for i in range(self.n_agents):
            current_v = self.critic[i](global_state_tensor[:, i], mean_actions_tensor[:, i]).squeeze(1)
            # calculate next mean action
            neighbors = self.env.get_obs(i)
            next_mean_action_tensor = th.zeros_like(mean_actions_tensor[:, i], device=self.device)
            for j in neighbors:
                next_mean_action_tensor += next_act[j]
            next_mean_action_tensor /= len(neighbors)
            next_v = self.critic_target[i](next_global_state_tensor[:, i], next_mean_action_tensor).squeeze(1)
            target_v = rewards_tensor[:, i] + self.reward_gamma * next_v * (1. - dones_tensor[:, i])
            # calculate vf loss
            self.critic_optimizer[i].zero_grad()
            if self.critic_loss == "huber":
                vf_loss = nn.SmoothL1Loss()(current_v, target_v.detach())
            else:
                vf_loss = nn.MSELoss()(current_v, target_v.detach())
            vf_loss.backward()

            # compute log action probs and target q
            actions_index = actions_index_tensor[:, i]
            actions_prob = self.actor[i](obs[i])
            log_action_prob = th.log(actions_prob.gather(1, actions_index.unsqueeze(1)).squeeze(1) + 1e-6)
            values = current_v.detach()
            # calculate pg loss and entropy
            self.actor_optimizer[i].zero_grad()
            pg_loss = th.mean(-log_action_prob * (target_v - values))
            neg_entropy = -th.mean(entropy(actions_prob))
            neg_entropy = 0

            # loss backward
            actor_loss = pg_loss + self.entropy_reg * neg_entropy
            actor_loss.backward()

            # optimizer step
            if self.max_grad_norm is not None:
                nn.utils.clip_grad_norm_(self.critic[i].parameters(), self.max_grad_norm)
                nn.utils.clip_grad_norm_(self.actor[i].parameters(), self.max_grad_norm)
            self.critic_optimizer[i].step()
            self.actor_optimizer[i].step()

        for i in range(self.n_agents):
            self._soft_update_target(self.critic_target[i], self.critic[i])
            self._soft_update_target(self.actor_target[i], self.actor[i])

    # get actions and mean actions by alternatively updating policy and mean action
    def mean_action(self, state):
        actions = np.zeros(self.n_agents, dtype='int')
        state_tensor = to_tensor(state, self.device).view(-1, self.n_agents, self.state_dim)
        mean_actions = np.zeros((self.n_agents, self.env.n_actions))
        epsilon = self.epsilon_end + (self.epsilon_start - self.epsilon_end) * \
                  np.exp(-1. * (self.n_episodes - self.episodes_before_train) / self.epsilon_decay)

        for i in range(self.n_agents):
            obs_tensor = state_tensor[:, i]
            if self.n_episodes < self.episodes_before_train or np.random.rand() < epsilon:
                actions[i] = np.random.choice(self.env.n_actions)
            else:
                actions_prob_tensor = self.actor[i](obs_tensor).squeeze(0)
                # actions_list = th.distributions.Categorical(actions_prob_tensor)
                # action = actions_list.sample()
                # actions[i] = action.item()
                actions[i] = th.argmax(actions_prob_tensor, dim=0).item()

        for i in range(self.n_agents):
            neighbors = self.env.get_obs(i)
            neighbor_actions = []
            for j in neighbors:
                action = index_to_one_hot(actions[j], self.action_dim)
                neighbor_actions.append(action)

            mean_actions[i] = np.mean(neighbor_actions, axis=0)

        return actions, mean_actions

    def action(self, state, evaluation=False, eval_records=None):
        actions = np.zeros(self.n_agents, dtype='int')
        state_tensor = to_tensor(state, self.device).view(-1, self.n_agents, self.state_dim)
        epsilon = self.epsilon_end + (self.epsilon_start - self.epsilon_end) * \
                  np.exp(-1. * (self.n_episodes - self.episodes_before_train) / self.epsilon_decay)

        for i in range(self.n_agents):
            obs_tensor = state_tensor[:, i]
            if evaluation:
                actions_prob_tensor = self.actor[i](obs_tensor).squeeze(0)
                actions[i] = th.argmax(actions_prob_tensor, dim=0).item()
                if i == 0:
                    max_values, indices = th.topk(actions_prob_tensor, 3)
                    row = np.hstack((max_values.detach().cpu().numpy(), indices.detach().cpu().numpy()))
                    eval_records.append(row)
            elif self.n_episodes < self.episodes_before_train or np.random.rand() < epsilon:
                actions[i] = np.random.choice(self.env.n_actions)
            else:
                actions_prob_tensor = self.actor[i](obs_tensor).squeeze(0)
                actions[i] = th.argmax(actions_prob_tensor, dim=0).item()

        return actions

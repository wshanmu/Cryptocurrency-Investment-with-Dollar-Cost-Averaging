import datetime
import math
import os
import random
from collections import deque
from typing import Deque, Dict, List, Tuple

import gym
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from IPython.display import clear_output
from torch.nn.utils import clip_grad_norm_

from rl_plotter.logger import Logger

from segment_tree import MinSegmentTree, SumSegmentTree


class ReplayBuffer:
    """A simple numpy replay buffer."""

    def __init__(
            self,
            obs_dim: int,
            size: int,
            batch_size: int = 32,
            n_step: int = 1,
            gamma: float = 0.99
    ):
        self.obs_buf = np.zeros([size, obs_dim], dtype=np.float32)
        self.next_obs_buf = np.zeros([size, obs_dim], dtype=np.float32)
        self.acts_buf = np.zeros([size], dtype=np.float32)
        self.rews_buf = np.zeros([size], dtype=np.float32)
        self.done_buf = np.zeros(size, dtype=np.float32)
        self.max_size, self.batch_size = size, batch_size
        self.ptr, self.size, = 0, 0

        # for N-step Learning
        self.n_step_buffer = deque(maxlen=n_step)
        self.n_step = n_step
        self.gamma = gamma

    def store(
            self,
            obs: np.ndarray,
            act: np.ndarray,
            rew: float,
            next_obs: np.ndarray,
            done: bool,
    ) -> Tuple[np.ndarray, np.ndarray, float, np.ndarray, bool]:
        transition = (obs, act, rew, next_obs, done)
        self.n_step_buffer.append(transition)

        # single step transition is not ready
        if len(self.n_step_buffer) < self.n_step:
            return ()

        # make a n-step transition
        rew, next_obs, done = self._get_n_step_info(
            self.n_step_buffer, self.gamma
        )
        obs, act = self.n_step_buffer[0][:2]

        self.obs_buf[self.ptr] = obs
        self.next_obs_buf[self.ptr] = next_obs
        self.acts_buf[self.ptr] = act
        self.rews_buf[self.ptr] = rew
        self.done_buf[self.ptr] = done
        self.ptr = (self.ptr + 1) % self.max_size
        self.size = min(self.size + 1, self.max_size)

        return self.n_step_buffer[0]

    def sample_batch(self) -> Dict[str, np.ndarray]:
        idxs = np.random.choice(self.size, size=self.batch_size, replace=False)

        return dict(
            obs=self.obs_buf[idxs],
            next_obs=self.next_obs_buf[idxs],
            acts=self.acts_buf[idxs],
            rews=self.rews_buf[idxs],
            done=self.done_buf[idxs],
            # for N-step Learning
            indices=idxs,
        )

    def sample_batch_from_idxs(
            self, idxs: np.ndarray
    ) -> Dict[str, np.ndarray]:
        # for N-step Learning
        return dict(
            obs=self.obs_buf[idxs],
            next_obs=self.next_obs_buf[idxs],
            acts=self.acts_buf[idxs],
            rews=self.rews_buf[idxs],
            done=self.done_buf[idxs],
        )

    def _get_n_step_info(
            self, n_step_buffer: Deque, gamma: float
    ) -> Tuple[np.int64, np.ndarray, bool]:
        """Return n step rew, next_obs, and done."""
        # info of the last transition
        rew, next_obs, done = n_step_buffer[-1][-3:]

        for transition in reversed(list(n_step_buffer)[:-1]):
            r, n_o, d = transition[-3:]

            rew = r + gamma * rew * (1 - d)
            next_obs, done = (n_o, d) if d else (next_obs, done)

        return rew, next_obs, done

    def __len__(self) -> int:
        return self.size


class PrioritizedReplayBuffer(ReplayBuffer):
    """Prioritized Replay buffer.

    Attributes:
        max_priority (float): max priority
        tree_ptr (int): next index of tree
        alpha (float): alpha parameter for prioritized replay buffer
        sum_tree (SumSegmentTree): sum tree for prior
        min_tree (MinSegmentTree): min tree for min prior to get max weight

    """

    def __init__(
            self,
            obs_dim: int,
            size: int,
            batch_size: int = 32,
            alpha: float = 0.6,
            n_step: int = 1,
            gamma: float = 0.99,
    ):
        """Initialization."""
        assert alpha >= 0

        super(PrioritizedReplayBuffer, self).__init__(
            obs_dim, size, batch_size, n_step, gamma
        )
        self.max_priority, self.tree_ptr = 1.0, 0
        self.alpha = alpha

        # capacity must be positive and a power of 2.
        tree_capacity = 1
        while tree_capacity < self.max_size:
            tree_capacity *= 2

        self.sum_tree = SumSegmentTree(tree_capacity)
        self.min_tree = MinSegmentTree(tree_capacity)

    def store(
            self,
            obs: np.ndarray,
            act: int,
            rew: float,
            next_obs: np.ndarray,
            done: bool,
    ) -> Tuple[np.ndarray, np.ndarray, float, np.ndarray, bool]:
        """Store experience and priority."""
        transition = super().store(obs, act, rew, next_obs, done)

        if transition:
            self.sum_tree[self.tree_ptr] = self.max_priority ** self.alpha
            self.min_tree[self.tree_ptr] = self.max_priority ** self.alpha
            self.tree_ptr = (self.tree_ptr + 1) % self.max_size

        return transition

    def sample_batch(self, beta: float = 0.4) -> Dict[str, np.ndarray]:
        """Sample a batch of experiences."""
        assert len(self) >= self.batch_size
        assert beta > 0

        indices = self._sample_proportional()

        obs = self.obs_buf[indices]
        next_obs = self.next_obs_buf[indices]
        acts = self.acts_buf[indices]
        rews = self.rews_buf[indices]
        done = self.done_buf[indices]
        weights = np.array([self._calculate_weight(i, beta) for i in indices])

        return dict(
            obs=obs,
            next_obs=next_obs,
            acts=acts,
            rews=rews,
            done=done,
            weights=weights,
            indices=indices,
        )

    def update_priorities(self, indices: List[int], priorities: np.ndarray):
        """Update priorities of sampled transitions."""
        assert len(indices) == len(priorities)

        for idx, priority in zip(indices, priorities):
            assert priority > 0
            assert 0 <= idx < len(self)

            self.sum_tree[idx] = priority ** self.alpha
            self.min_tree[idx] = priority ** self.alpha

            self.max_priority = max(self.max_priority, priority)

    def _sample_proportional(self) -> List[int]:
        """Sample indices based on proportions."""
        indices = []
        p_total = self.sum_tree.sum(0, len(self) - 1)
        segment = p_total / self.batch_size

        for i in range(self.batch_size):
            a = segment * i
            b = segment * (i + 1)
            upperbound = random.uniform(a, b)
            idx = self.sum_tree.retrieve(upperbound)
            indices.append(idx)

        return indices

    def _calculate_weight(self, idx: int, beta: float):
        """Calculate the weight of the experience at idx."""
        # get max weight
        p_min = self.min_tree.min() / self.sum_tree.sum()
        max_weight = (p_min * len(self)) ** (-beta)

        # calculate weights
        p_sample = self.sum_tree[idx] / self.sum_tree.sum()
        weight = (p_sample * len(self)) ** (-beta)
        weight = weight / max_weight

        return weight


class NoisyLinear(nn.Module):
    """Noisy linear module for NoisyNet.



    Attributes:
        in_features (int): input size of linear module
        out_features (int): output size of linear module
        std_init (float): initial std value
        weight_mu (nn.Parameter): mean value weight parameter
        weight_sigma (nn.Parameter): std value weight parameter
        bias_mu (nn.Parameter): mean value bias parameter
        bias_sigma (nn.Parameter): std value bias parameter

    """

    def __init__(
            self,
            in_features: int,
            out_features: int,
            std_init: float = 0.5,
    ):
        """Initialization."""
        super(NoisyLinear, self).__init__()

        self.in_features = in_features
        self.out_features = out_features
        self.std_init = std_init

        self.weight_mu = nn.Parameter(torch.Tensor(out_features, in_features))
        self.weight_sigma = nn.Parameter(
            torch.Tensor(out_features, in_features)
        )
        self.register_buffer(
            "weight_epsilon", torch.Tensor(out_features, in_features)
        )

        self.bias_mu = nn.Parameter(torch.Tensor(out_features))
        self.bias_sigma = nn.Parameter(torch.Tensor(out_features))
        self.register_buffer("bias_epsilon", torch.Tensor(out_features))

        self.reset_parameters()
        self.reset_noise()

    def reset_parameters(self):
        """Reset trainable network parameters (factorized gaussian noise)."""
        mu_range = 1 / math.sqrt(self.in_features)
        self.weight_mu.data.uniform_(-mu_range, mu_range)
        self.weight_sigma.data.fill_(
            self.std_init / math.sqrt(self.in_features)
        )
        self.bias_mu.data.uniform_(-mu_range, mu_range)
        self.bias_sigma.data.fill_(
            self.std_init / math.sqrt(self.out_features)
        )

    def reset_noise(self):
        """Make new noise."""
        epsilon_in = self.scale_noise(self.in_features)
        epsilon_out = self.scale_noise(self.out_features)

        # outer product
        self.weight_epsilon.copy_(epsilon_out.ger(epsilon_in))
        self.bias_epsilon.copy_(epsilon_out)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward method implementation.

        We don't use separate statements on train / eval mode.
        It doesn't show remarkable difference of performance.
        """
        return F.linear(
            x,
            self.weight_mu + self.weight_sigma * self.weight_epsilon,
            self.bias_mu + self.bias_sigma * self.bias_epsilon,
        )

    @staticmethod
    def scale_noise(size: int) -> torch.Tensor:
        """Set scale to make noise (factorized gaussian noise)."""
        x = torch.randn(size)

        return x.sign().mul(x.abs().sqrt())


class Network(nn.Module):
    def __init__(
            self,
            in_dim: int,
            out_dim: int,
            atom_size: int,
            support: torch.Tensor
    ):
        """Initialization."""
        super(Network, self).__init__()

        self.support = support
        self.out_dim = out_dim
        self.atom_size = atom_size

        # set common feature layer
        self.feature_layer = nn.Sequential(
            nn.Linear(in_dim, 128),
            nn.ReLU(),
        )

        # set advantage layer
        self.advantage_hidden_layer = NoisyLinear(128, 128)
        self.advantage_layer = NoisyLinear(128, out_dim * atom_size)

        # set value layer
        self.value_hidden_layer = NoisyLinear(128, 128)
        self.value_layer = NoisyLinear(128, atom_size)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward method implementation."""
        dist = self.dist(x)
        q = torch.sum(dist * self.support, dim=2)

        return q

    def dist(self, x: torch.Tensor) -> torch.Tensor:
        """Get distribution for atoms."""
        feature = self.feature_layer(x)
        adv_hid = F.relu(self.advantage_hidden_layer(feature))
        val_hid = F.relu(self.value_hidden_layer(feature))

        advantage = self.advantage_layer(adv_hid).view(
            -1, self.out_dim, self.atom_size
        )
        value = self.value_layer(val_hid).view(-1, 1, self.atom_size)
        q_atoms = value + advantage - advantage.mean(dim=1, keepdim=True)

        dist = F.softmax(q_atoms, dim=-1)
        dist = dist.clamp(min=1e-3)  # for avoiding nans

        return dist

    def reset_noise(self):
        """Reset all noisy layers."""
        self.advantage_hidden_layer.reset_noise()
        self.advantage_layer.reset_noise()
        self.value_hidden_layer.reset_noise()
        self.value_layer.reset_noise()


class DQNAgent:
    """DQN Agent interacting with environment.

    Attribute:
        env (gym.Env): openAI Gym environment
        memory (PrioritizedReplayBuffer): replay memory to store transitions
        batch_size (int): batch size for sampling
        target_update (int): period for target model's hard update
        gamma (float): discount factor
        dqn (Network): model to train and select actions
        dqn_target (Network): target model to update
        optimizer (torch.optim): optimizer for training dqn
        transition (list): transition information including
                           state, action, reward, next_state, done
        v_min (float): min value of support
        v_max (float): max value of support
        atom_size (int): the unit number of support
        support (torch.Tensor): support for categorical dqn
        use_n_step (bool): whether to use n_step memory
        n_step (int): step number to calculate n-step td error
        memory_n (ReplayBuffer): n-step replay buffer
    """

    def __init__(
            self,
            env: gym.Env,
            memory_size: int,
            batch_size: int,
            target_update: int,
            gamma: float = 0.99,
            # PER parameters
            alpha: float = 0.2,
            beta: float = 0.6,
            prior_eps: float = 1e-6,
            # Categorical DQN parameters
            v_min: float = 0.0,
            v_max: float = 200.0,
            atom_size: int = 51,
            # N-step Learning
            n_step: int = 3,
    ):
        """Initialization.

        Args:
            env (gym.Env): openAI Gym environment
            memory_size (int): length of memory
            batch_size (int): batch size for sampling
            target_update (int): period for target model's hard update
            lr (float): learning rate
            gamma (float): discount factor
            alpha (float): determines how much prioritization is used
            beta (float): determines how much importance sampling is used
            prior_eps (float): guarantees every transition can be sampled
            v_min (float): min value of support
            v_max (float): max value of support
            atom_size (int): the unit number of support
            n_step (int): step number to calculate n-step td error
        """
        obs_dim = env.observation_space.shape[1]
        action_dim = env.action_space.n

        self.env = env
        self.batch_size = batch_size
        self.target_update = target_update
        self.gamma = gamma
        # NoisyNet: All attributes related to epsilon are removed

        # device: cpu / gpu
        self.device = torch.device(
            "cuda" if torch.cuda.is_available() else "cpu"
        )
        print(self.device)

        # PER
        # memory for 1-step Learning
        self.beta = beta
        self.prior_eps = prior_eps
        self.memory = PrioritizedReplayBuffer(
            obs_dim, memory_size, batch_size, alpha=alpha
        )

        # memory for N-step Learning
        self.use_n_step = True if n_step > 1 else False
        if self.use_n_step:
            self.n_step = n_step
            self.memory_n = ReplayBuffer(
                obs_dim, memory_size, batch_size, n_step=n_step, gamma=gamma
            )

        # Categorical DQN parameters
        self.v_min = v_min
        self.v_max = v_max
        self.atom_size = atom_size
        self.support = torch.linspace(
            self.v_min, self.v_max, self.atom_size
        ).to(self.device)

        # networks: dqn, dqn_target
        self.dqn = Network(
            obs_dim, action_dim, self.atom_size, self.support
        ).to(self.device)
        self.dqn_target = Network(
            obs_dim, action_dim, self.atom_size, self.support
        ).to(self.device)
        self.dqn_target.load_state_dict(self.dqn.state_dict())
        self.dqn_target.eval()

        # optimizer
        self.optimizer = optim.Adam(self.dqn.parameters(), lr=5e-4)

        # transition to store in memory
        self.transition = list()

        # mode: train / test
        self.is_test = False

    def select_action(self, state: np.ndarray) -> np.ndarray:
        """Select an action from the input state."""
        # NoisyNet: no epsilon greedy action selection
        selected_action = self.dqn(
            torch.FloatTensor(state).to(self.device)
        ).argmax()
        selected_action = selected_action.detach().cpu().numpy()

        if not self.is_test:
            self.transition = [state, selected_action]

        return selected_action

    def step(self, action: np.ndarray) -> Tuple[np.ndarray, np.float64, bool]:
        """Take an action and return the response of the env."""
        next_state, reward, done, _ = self.env.step(action)

        if not self.is_test:
            self.transition += [reward, next_state, done]

            # N-step transition
            if self.use_n_step:
                one_step_transition = self.memory_n.store(*self.transition)
            # 1-step transition
            else:
                one_step_transition = self.transition

            # add a single step transition
            if one_step_transition:
                self.memory.store(*one_step_transition)

        return next_state, reward, done

    def update_model(self) -> torch.Tensor:
        """Update the model by gradient descent."""
        # PER needs beta to calculate weights
        samples = self.memory.sample_batch(self.beta)
        weights = torch.FloatTensor(
            samples["weights"].reshape(-1, 1)
        ).to(self.device)
        indices = samples["indices"]

        # 1-step Learning loss
        elementwise_loss = self._compute_dqn_loss(samples, self.gamma)

        # PER: importance sampling before average
        loss = torch.mean(elementwise_loss * weights)

        # N-step Learning loss
        # we are gonna combine 1-step loss and n-step loss so as to
        # prevent high-variance. The original rainbow employs n-step loss only.
        if self.use_n_step:
            gamma = self.gamma ** self.n_step
            samples = self.memory_n.sample_batch_from_idxs(indices)
            elementwise_loss_n_loss = self._compute_dqn_loss(samples, gamma)
            elementwise_loss += elementwise_loss_n_loss

            # PER: importance sampling before average
            loss = torch.mean(elementwise_loss * weights)

        self.optimizer.zero_grad()
        loss.backward()
        clip_grad_norm_(self.dqn.parameters(), 10.0)
        self.optimizer.step()

        # PER: update priorities
        loss_for_prior = elementwise_loss.detach().cpu().numpy()
        new_priorities = loss_for_prior + self.prior_eps
        self.memory.update_priorities(indices, new_priorities)

        # NoisyNet: reset noise
        self.dqn.reset_noise()
        self.dqn_target.reset_noise()

        return loss.item()

    def train(self, logger, num_frames: int, plotting_interval: int = 200):
        """Train the agent."""
        self.is_test = False

        state = self.env.reset()
        update_cnt = 0
        losses = []
        scores = []
        mean_scores = []
        score = 0

        for frame_idx in range(1, num_frames + 1):
            action = self.select_action(state)
            next_state, reward, done = self.step(action)

            state = next_state
            score += reward

            # NoisyNet: removed decrease of epsilon

            # PER: increase beta
            fraction = min(frame_idx / num_frames, 1.0)
            self.beta = self.beta + fraction * (1.0 - self.beta)

            # if episode ends
            if done:
                state = self.env.reset()
                scores.append(score)
                logger.update(score=scores, total_steps=frame_idx)
                if len(scores) >= 20:
                    mean_scores.append(np.mean(scores[-20:]))
                score = 0

            # if training is ready
            if len(self.memory) >= self.batch_size:
                loss = self.update_model()
                losses.append(loss)
                update_cnt += 1

                # if hard update is needed
                if update_cnt % self.target_update == 0:
                    self._target_hard_update()

            # plotting
            if frame_idx % plotting_interval == 0:
                self._plot(frame_idx, scores, losses, mean_scores)
            if frame_idx % 2000 == 0:
                print("Step: %d, Mean Score: %.2f" % (frame_idx, np.mean(np.array(mean_scores[-20:]))))

        self.env.close()

    def test(self) -> Tuple[float, float]:
        """Test the agent."""
        self.is_test = True

        # for recording a video
        naive_env = self.env
        # self.env = gym.wrappers.RecordVideo(self.env, video_folder=video_folder)

        state = self.env.reset()
        done = False
        score = 0

        while not done:
            action = self.select_action(state)
            next_state, reward, done = self.step(action)

            state = next_state
            score += reward
        price = 1 / (1 + np.exp(reward))
        # print("normalized price: %.3f" % price)
        # print("score: ", score)
        self.env.close()

        # reset
        self.env = naive_env
        return price, score

    def _compute_dqn_loss(self, samples: Dict[str, np.ndarray], gamma: float) -> torch.Tensor:
        """Return categorical dqn loss."""
        device = self.device  # for shortening the following lines
        state = torch.FloatTensor(samples["obs"]).to(device)
        next_state = torch.FloatTensor(samples["next_obs"]).to(device)
        action = torch.LongTensor(samples["acts"]).to(device)
        reward = torch.FloatTensor(samples["rews"].reshape(-1, 1)).to(device)
        done = torch.FloatTensor(samples["done"].reshape(-1, 1)).to(device)

        # Categorical DQN algorithm
        delta_z = float(self.v_max - self.v_min) / (self.atom_size - 1)

        with torch.no_grad():
            # Double DQN
            next_action = self.dqn(next_state).argmax(1)
            next_dist = self.dqn_target.dist(next_state)
            next_dist = next_dist[range(self.batch_size), next_action]

            t_z = reward + (1 - done) * gamma * self.support
            t_z = t_z.clamp(min=self.v_min, max=self.v_max)
            b = (t_z - self.v_min) / delta_z
            l = b.floor().long()
            u = b.ceil().long()

            offset = (
                torch.linspace(
                    0, (self.batch_size - 1) * self.atom_size, self.batch_size
                ).long()
                .unsqueeze(1)
                .expand(self.batch_size, self.atom_size)
                .to(self.device)
            )

            proj_dist = torch.zeros(next_dist.size(), device=self.device)
            proj_dist.view(-1).index_add_(
                0, (l + offset).view(-1), (next_dist * (u.float() - b)).view(-1)
            )
            proj_dist.view(-1).index_add_(
                0, (u + offset).view(-1), (next_dist * (b - l.float())).view(-1)
            )

        dist = self.dqn.dist(state)
        log_p = torch.log(dist[range(self.batch_size), action])
        elementwise_loss = -(proj_dist * log_p).sum(1)

        return elementwise_loss

    def _target_hard_update(self):
        """Hard update: target <- local."""
        self.dqn_target.load_state_dict(self.dqn.state_dict())

    def _plot(
            self,
            frame_idx: int,
            scores: List[float],
            losses: List[float],
            mean_scores: List[float]
    ):
        """Plot the training progresses."""
        clear_output(True)
        plt.figure(figsize=(15, 12))
        plt.title('Score v.s Episodes', fontsize=15)
        # plt.plot(scores, label='scores')
        plt.plot(mean_scores, label='mean')
        # plt.legend(fontsize=13)
        plt.xlabel('Episodes', fontsize=14)
        plt.ylabel('Score', fontsize=14)
        plt.xticks(fontsize=13)
        plt.yticks(fontsize=13)
        # plt.show()
        plt.savefig('./figresults/%sRainbow_Score_Num_%d' % (Expetiment_ID, num_frames))

        plt.figure(figsize=(16, 9))
        plt.title('Loss v.s Steps', fontsize=15)
        plt.plot(losses)
        plt.xlabel('Steps', fontsize=14)
        plt.ylabel('Loss', fontsize=14)
        plt.xticks(fontsize=13)
        plt.yticks(fontsize=13)
        plt.savefig('./figresults/%sRainbow_Loss_Num_%d' % (Expetiment_ID, num_frames))

        a = np.array(mean_scores)
        np.save('./figresults/%s_mean_scores_rainbow_%d.npy' % (Expetiment_ID, num_frames), a)
        a = np.array(losses)
        np.save('./figresults/%sloss_rainbow_%d.npy' % (Expetiment_ID, num_frames), a)

import pickle

def dateAdd(date, interval=1):
    dt = datetime.datetime.strptime(date, "%Y%m%d")
    dt = dt + datetime.timedelta(interval)
    date1 = dt.strftime("%Y%m%d")
    return date1


def GetPriceList(content, name_num=0):
    price_list = []
    name_list = ['BTCBitcoin_price', 'ETHEthereum_price']
    desired = name_list[name_num]
    cnt = 0
    for name in content:
        if desired in name:
            if cnt == 0:
                start_date = name[0:8]
                cnt += 1
            price_list.append(content[name])
    return price_list, start_date


data_file = open(r'../data/Data.pkl', 'rb')
content = pickle.load(data_file)
total_data, start_date = GetPriceList(content, name_num=0)  # 0 for BTC, 1 for ETH

useful_data = total_data[500:]
train_data = useful_data[0:int(len(useful_data) * 0.80)]
test_data = useful_data[-int(len(useful_data) * 0.20):]
print("Total data date range: %s to %s, %d days" % (start_date, dateAdd(start_date, len(total_data)), len(total_data)))
print("Train data date range: %s to %s, %d days" % (
    dateAdd(start_date, 50), dateAdd(start_date, 50 + len(train_data)), len(train_data)))
print("Test data date range: %s to %s %d days" % (
    dateAdd(start_date, 50 + len(train_data) + 1), dateAdd(start_date, 50 + len(train_data) + 1 + len(test_data)),
    len(test_data)))

Expetiment_ID = '1106BTC'

wnd_t = 30
cycle_T = 9
env_id = 'CryptoEnv-v0'
env = gym.make('CryptoEnv-v0', data=train_data, wnd_t=wnd_t, cycle_T=cycle_T)


def seed_torch(seed):
    torch.manual_seed(seed)
    if torch.backends.cudnn.enabled:
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True


# parameters
num_frames = 500 * 1000
memory_size = 1000
batch_size = 128
target_update = 200


def rainbowtrain(num_frames):
    seed_list = [111 * i for i in range(1, 6)]
    for seed in seed_list:
        logger = Logger(exp_name="New_Rainbow", env_name="BTC", seed=seed)
        np.random.seed(seed)
        random.seed(seed)
        seed_torch(seed)
        env.seed(seed)
        agent = DQNAgent(env, memory_size, batch_size, target_update)
        agent.train(logger, num_frames, plotting_interval=num_frames)
        scores = []
        prices = []
        for i in range(5000):
            price, score = agent.test()
            scores.append(score)
            prices.append(price)
        print("Average normalized price: %.3f" % np.mean(np.array(prices)))
        print("Average score: %.3f" % np.mean(np.array(scores)))

        torch.save(agent.dqn,
                   '%sSeed%d_%s_%d_%d_Step_%d.pth' % (Expetiment_ID, seed, 'Rainbow', wnd_t, cycle_T, num_frames))


import random


def sigmoid(inx):
    if inx >= 0:  # 对sigmoid函数的优化，避免了出现极大的数据溢出
        return 1.0 / (1 + np.exp(-inx))
    else:
        return np.exp(inx) / (1 + np.exp(inx))


def rainbowevaluate():
    F = open(r'D://SJTU-STUDY//Research//NUS//Data//Eva_Data.pkl', 'rb')
    content = pickle.load(F)
    total_data, start_date = GetPriceList(content, name_num=0)  # get BTC prince data
    print(start_date)
    # test_data = total_data[:]
    # test_data = total_data[-500:]
    print(len(total_data[1300:]) * 0.1)
    test_data_list = [total_data[-218:], total_data[-49:-9], total_data[-58:-18]]
    # test_data = total_data[-int(len(total_data)*0.3):]
    seed = 777
    np.random.seed(seed)
    random.seed(seed)
    seed_torch(seed)
    env.seed(seed)
    agent = DQNAgent(env, memory_size, batch_size, target_update)
    agent.dqn = torch.load('D://SJTU-STUDY//Research//NUS//Optimal Stopping//RLforDAC//rainbow//good//46.pth')
    agent.dqn.eval()

    for test_data in test_data_list:
        test_env = gym.make('CryptoEnv-v0', data=test_data, wnd_t=wnd_t, cycle_T=cycle_T)
        ev_episodes = test_env.prepare_episodes()
        original_episodes = test_env.prepare_original_episodes()

        e = 0
        random_list = []
        p_list = []
        first_day = []
        last_day = []
        ratio1 = []
        ratio2 = []
        ratio3 = []
        t_list = []
        visual_price = []
        for episode in ev_episodes:
            refer_value = original_episodes[e][0][-2]
            t = 0
            for state in episode:
                remain_t = (cycle_T - t) / cycle_T
                price = original_episodes[e][t][-1]
                position_value = sigmoid(price - refer_value)
                obs = (np.concatenate(([position_value, remain_t], state)))
                obs = obs.reshape(1, wnd_t + 2)

                action = agent.dqn(
                    torch.FloatTensor(obs).to('cuda')
                ).argmax()
                action = action.detach().cpu().numpy()

                if action == 1 or t == cycle_T - 1:
                    p_list.append(original_episodes[e][t][-1])
                    first_day.append(original_episodes[e][0][-1])
                    last_day.append(original_episodes[e][-1][-1])
                    random_t = random.randint(0, cycle_T - 1)
                    random_list.append(original_episodes[e][random_t][-1])
                    period_price_list = [original_episodes[e][i][-1] for i in range(cycle_T)]

                    if e % cycle_T == 0:
                        # print('current e:%d'%e)
                        t_list.append(int(wnd_t + cycle_T * int(e // cycle_T) + t - 1))
                        visual_price.append(original_episodes[e][t][-1])
                    break
                t += 1
            e += 1
            p_amount = np.sum(np.array([10000 / i for i in p_list]))
            f_amount = np.sum(np.array([10000 / i for i in first_day]))
            l_amount = np.sum(np.array([10000 / i for i in last_day]))
            r_amount = np.sum(np.array([10000 / i for i in random_list]))
            ratio1.append((p_amount - f_amount) / f_amount * 100)
            ratio2.append((p_amount - l_amount) / l_amount * 100)
            ratio3.append((p_amount - r_amount) / r_amount * 100)
        print("Compared with always buy on the first day: %.2f %%" % np.mean(np.array(ratio1)))
        print("Compared with always buy on the last day: %.2f %%" % np.mean(np.array(ratio2)))
        print("Compared with always buy on a random day: %.2f %%" % np.mean(np.array(ratio3)))
        # plt.plot(test_data)
        # plt.show()

        plt.figure(1, figsize=[16, 9])
        plt.plot(range(len(test_data)), test_data, color='b', label='Price')
        print(len(t_list))
        for i in range(len(t_list) + 1):
            plt.axvline(x=i * cycle_T + wnd_t - 1, c='g', ls='--', lw=1)  # vertical
        # for i in range(int((len(test_data)-wnd_t)//cycle_T)+1):
        #     plt.axvline(x=i * cycle_T + wnd_t, c='g', ls='--', lw=1)  # vertical
        # for j in range(len(t_list)):
        #     plt.scatter(t_list[j], test_data[t_list[j]], s=20, c='r')  # stroke, colour
        for j in range(len(t_list)):
            plt.scatter(t_list[j], visual_price[j], s=20, c='r')  # stroke, colour
        plt.title("The performance of the DQN", fontsize=15)
        plt.legend()
        plt.show()


def date_range(beginDate, endDate, interval=1):
    dates = []
    dt = datetime.datetime.strptime(beginDate, "%Y%m%d")
    date = beginDate[:]
    while date <= endDate:
        dates.append(date)
        dt = dt + datetime.timedelta(interval)
        date = dt.strftime("%Y%m%d")
    return dates


def dateAdd(date, interval=1):
    dt = datetime.datetime.strptime(date, "%Y%m%d")
    dt = dt + datetime.timedelta(interval)
    date1 = dt.strftime("%Y%m%d")
    return date1


def SingleRainbowEvaluate(path, stat=136, model_id=47, T=9, num=0):
    F = open(r'../data/Eva_Data.pkl', 'rb')
    content = pickle.load(F)
    total_data, start_date = GetPriceList(content, name_num=num)  # 0 for BTC price data
    end_date = dateAdd(start_date, len(total_data))

    # stat = 127
    test_data_list = [total_data[-(stat + 30):]]
    seed = 777
    np.random.seed(seed)
    random.seed(seed)
    seed_torch(seed)
    env.seed(seed)
    agent = DQNAgent(env, memory_size, batch_size, target_update)
    agent.dqn = torch.load(path)
    agent.dqn.eval()

    cycle_T = T

    for test_data in test_data_list:
        test_env = gym.make('CryptoEnv-v0', data=test_data, wnd_t=wnd_t, cycle_T=cycle_T)
        ev_episodes = test_env.prepare_episodes()
        original_episodes = test_env.prepare_original_episodes()

        e = 0
        random_list = []
        p_list = []
        first_day = []
        last_day = []
        avg_day = []
        ratio1 = []
        ratio2 = []
        ratio3 = []
        ratio4 = []
        t_list = []
        visual_price = []

        for episode in ev_episodes:
            if e % cycle_T != 0:
                e += 1
                continue
            refer_value = original_episodes[e][0][-2]
            t = 0
            for state in episode:
                remain_t = (cycle_T - t) / cycle_T
                price = original_episodes[e][t][-1]
                position_value = sigmoid(price - refer_value)
                obs = (np.concatenate(([position_value, remain_t], state)))
                obs = obs.reshape(1, wnd_t + 2)

                action = agent.dqn(
                    torch.FloatTensor(obs).to('cuda')
                ).argmax()
                action = action.detach().cpu().numpy()

                if action == 1 or t == cycle_T - 1:
                    p_list.append(original_episodes[e][t][-1])
                    first_day.append(original_episodes[e][0][-1])
                    last_day.append(original_episodes[e][-1][-1])
                    random_t = random.randint(0, cycle_T - 1)
                    random_list.append(original_episodes[e][random_t][-1])
                    period_price_list = [original_episodes[e][i][-1] for i in range(cycle_T)]
                    avg_day.append(np.mean(np.array(period_price_list)))
                    if e % cycle_T == 0:
                        t_list.append(int(wnd_t + cycle_T * int(e // cycle_T) + t - 1))
                        if original_episodes[e][t][-1] >= np.mean(np.array(period_price_list)):
                            visual_price.append(original_episodes[e][t][-1])
                        else:
                            visual_price.append(-original_episodes[e][t][-1])
                    break
                t += 1
            e += 1
        p_amount = np.sum(np.array([10000 / i for i in p_list]))
        p_amount_list = np.array([10000 / i for i in p_list])
        f_amount = np.sum(np.array([10000 / i for i in first_day]))
        f_amount_list = (np.array([10000 / i for i in first_day]))
        l_amount = np.sum(np.array([10000 / i for i in last_day]))
        l_amount_list = (np.array([10000 / i for i in last_day]))
        r_amount = np.sum(np.array([10000 / i for i in random_list]))
        avg_amount = np.sum(np.array([10000 / i for i in avg_day]))
        avg_amount_list = (np.array([10000 / i for i in avg_day]))
        ratio1.append((p_amount - f_amount) / f_amount * 100)
        ratio2.append((p_amount - l_amount) / l_amount * 100)
        ratio3.append((p_amount - r_amount) / r_amount * 100)
        ratio4.append((p_amount - avg_amount) / avg_amount * 100)
        print("Compared with always buy on the first day: %.2f %%" % np.mean(np.array(ratio1)))
        print("Compared with always buy on the last day: %.2f %%" % np.mean(np.array(ratio2)))
        print("Compared with always buy on a random day: %.2f %%" % np.mean(np.array(ratio3)))
        print("Compared with buy on the average price: %.2f %%" % np.mean(np.array(ratio4)))

        # plt.figure(1, figsize=[16, 9])
        # plt.plot(range(len(test_data)), test_data, label='Price')

        test_list_data = date_range(dateAdd(end_date, -(stat + 5)), dateAdd(end_date, -1))
        print_date_list = [str(i) for i in test_list_data]
        print_date_list = [i[0:4]+'/'+i[4:6]+'/'+i[6:] for i in print_date_list]
        print(print_date_list)

        fig = plt.figure(figsize=(15, 7))
        ax = fig.add_subplot(111)
        # plt.title("Test data", fontsize=16)
        plt.ylabel('The price of ETH', fontsize=15)
        plt.yticks(fontsize=14)
        plt.xlabel('Datetime', fontsize=15)
        xs = [datetime.datetime.strptime(d, '%Y%m%d').date() for d in test_list_data]
        # plt.xticks(ticks=pd.date_range('2022-08-29', '2022-11-07', freq='1d'), fontsize=12)
        ax.plot(xs, test_data_list[0][25:], color='black', label='ETH')
        print(test_data_list[0][25:])
        print(len(test_data_list[0][25:]))
        i = 0
        up = 200
        while 5 + (i + 1) * T <= len(xs):
            plt.fill_between(xs[5 + i * T - 1:5 + (i + 1) * T], min(test_data[25:]) - up, max(test_data[25:]) + up,
                             facecolor='grey', alpha=0.2)
            plt.fill_between(xs[5 + (i + 1) * T - 1:5 + (i + 2) * T], min(test_data[25:]) - up, max(test_data[25:]) + up,
                             facecolor='grey', alpha=0.1)
            i += 2

        # for i in range(len(t_list) + 1):
        #     plt.axvline(x=i * cycle_T + wnd_t - 1, c='g', ls='--', lw=1)  # vertical
        # # for i in range(int((len(test_data)-wnd_t)//cycle_T)+1):
        # #     plt.axvline(x=i * cycle_T + wnd_t, c='g', ls='--', lw=1)  # vertical
        # # for j in range(len(t_list)):
        # #     plt.scatter(t_list[j], test_data[t_list[j]], s=20, c='r')  # stroke, colour
        for j in range(len(t_list)):
            print(xs[t_list[j] - 25])
            if visual_price[j] > 0:
                plt.scatter(xs[t_list[j] - 25], visual_price[j], s=20, c='g')  # stroke, colour    
            else:
                plt.scatter(xs[t_list[j] - 25], -visual_price[j], s=20, c='r')
        plt.title("The performance on ETH test data", fontsize=17)
        plt.legend(fontsize=14)
        # plt.show()

        plt.savefig("./T_%d_ETH_Interactive_%s.jpg" % (cycle_T, model_id))
        plt.close()


if __name__ == '__main__':
    # rainbowtrain(num_frames)
    # rainbowevaluate()
    
    # index_list = [78]
    # for i in index_list:
    #     print("Currently i = %d" % i)
    #     SingleRainbowEvaluate(path='./models/ETH_0.5Reward_Gamma0_95_Rainbow_30_9/Seed888_Step_500k/%s.pth' % i,
    #                           stat= 127, model_id=i)
    #     print(" ")

    T_list = range(9, 10)
    # for T in T_list:
    #     model_id = 13
    #     print("Currently T = %d" % T)
    #     SingleRainbowEvaluate(path='./models/NNNdata_BTC_0.5Reward_Gamma0_95_Rainbow_30_9/Seed777_Step_300k/%s.pth' % model_id,
    #                           stat= 136, model_id=model_id, T=T, num=1)
    #     print(" ")

    for T in T_list:
        model_id = 35
        print("Currently T = %d" % T)
        SingleRainbowEvaluate(path='./models/ETH_0.5Reward_Gamma0_95_Rainbow_30_9/Seed999_Step_500k/%s.pth' % model_id,
                              stat= 127, model_id=model_id, T=T, num=1)
        print(" ")

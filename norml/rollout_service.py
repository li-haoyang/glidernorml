# coding=utf-8
# Copyright 2020 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Perform rollouts locally."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import numpy as np
import tensorflow.compat.v1 as tf
from glidernorml.norml import tools


class RolloutServiceLocal(object):
  """Performs rollouts for MAML."""

  def __init__(self, config):
    self.config = config
    self._batch_envs = {}
    self.max_num_batch_env = self.config.max_num_batch_env
    self.task_generator = self.config.task_generator
    self.always_full_rollouts = self.config.always_full_rollouts
    self.max_rollout_len = self.config.max_rollout_len
    self.action_to_index = True#for glider,pick action index to env(sender) !not used,deal in batch_enc.py

  def _get_batch_env(self, task_modifier, num_rollouts):
    """Creates or reuses a batch environment for a task.

    Args:
      task_modifier: the modifier (dict of attribute, value pairs) for the task.
      num_rollouts: number of parallel tasks (multiprocessing).

    Returns:
      A batch environment that acts like a gym environment.
    """
    if num_rollouts not in self._batch_envs:
      # find out if space available
      def compute_num_batch_envs():
        return np.sum(list(self._batch_envs.keys()))

      # remove items until space available
      num_deleted = 0
      while (compute_num_batch_envs() >
             self.max_num_batch_env - num_rollouts) and len(self._batch_envs):
        _, del_env = self._batch_envs.popitem()
        del_env.close()
        del del_env
        num_deleted += 1

      def wrap_task_generator(task_generator):
        """Provides getter and setter functions to access wrapped environments."""
        wrap = tools.wrappers
        #for glider, instantiate sender and handshake before wrapped into a process
        task_generator_instance = task_generator()
        print("_______________hand shake")
        task_generator_instance.handshake()
        print("_______________hand shake finish")
        #limit_duration_env = wrap.LimitDuration(task_generator(),
                                                #self.max_rollout_len)#add duration limit to rollout
        limit_duration_env = wrap.LimitDuration(task_generator_instance,
                                                self.max_rollout_len)#add duration limit to rollout
        range_normalize_env = wrap.RangeNormalize(limit_duration_env)#normalize env states and rewars
        clip_action_env = wrap.ClipAction(range_normalize_env)#when action is out of range, cut it into spaces
        external_process_env = wrap.ExternalProcess(lambda: clip_action_env)
        return external_process_env

      envs = []
      for _ in range(num_rollouts):
        #envs.append(wrap_task_generator(self.task_generator))#TODO:Check if wrapper is useful
        envs.append(wrap_task_generator(self.task_generator))
        '''envs.append(self.task_generator)'''
      self._batch_envs[num_rollouts] = tools.BatchEnv(envs, blocking=False)

    batch_env = self._batch_envs[num_rollouts]
    # apply task_modifier(meant to creat different tasks, make model )
    for attr in task_modifier:
      batch_env.set_attribute(attr, task_modifier[attr], single=True)#add modifier to env
    return batch_env

  def perform_rollouts(self,
                       session,
                       num_parallel_rollouts,
                       policy,
                       task_modifier,
                       sample_vars=None):
    """Generate samples from multiple policy rollouts.

    Args:
      session: tf session.
      num_parallel_rollouts: number of parallel rollout processes（To speed up computation, our MAML and NoRML implementations are parallelized across tasks and rollouts.）
      policy: policy to deploy.
      task_modifier: gym env modifier function.
      sample_vars: dict with arguments to pass to the sampling function (tf).

    Returns:
      rollouts: dict per rollout:
        timesteps: numpy array of timesteps and total rollout length:
          [(0, 200), (1, 200)...]
        states: numpy array of states (t_0...t_N): (N+1)xN_states
        actions: numpy array of actions (t_0...t_N-1): NxN_actions
        rewards: numpy array of rewards (t_0...t_N-1): Nx1
    """
    batch_env = self._get_batch_env(task_modifier, num_parallel_rollouts)#NOTI:make a midified gym env
    state = batch_env.reset().reshape((num_parallel_rollouts, -1))
    print('[norml_roolout]_initState',state)
    states = [[state[idx]] for idx in range(num_parallel_rollouts)]
    actions = [[] for _ in range(num_parallel_rollouts)]#make place holder
    rewards = [[] for _ in range(num_parallel_rollouts)]
    sample_op, state_var = policy.sample_op()
    
    #batch_env.handshake()#for glider, handshake all senders before start perform inner rollout
    completed_states = []
    completed_actions = []
    completed_rewards = []
    if sample_vars is None:
      sample_vars = {}

    step = 0#rollOut Step
    while step < self.max_rollout_len:#run the task
      step += 1
      sample_vars[state_var] = state
      action = session.run(sample_op, sample_vars)#feed state and fetch sample_op
      action = np.nan_to_num(action, copy=False)
      new_state, reward, new_tasks_done, _ = batch_env.step(action)#take action
      print('[norml_rollout]New State:',new_state)
      for idx in range(num_parallel_rollouts):
        states[idx].append(new_state[idx:idx + 1].ravel())
        actions[idx].append(action[idx:idx + 1, :])
        rewards[idx].append(reward[idx])
        if new_tasks_done[idx]:
          completed_states.append(states[idx])
          completed_actions.append(actions[idx])
          completed_rewards.append(rewards[idx])
          states[idx] = [batch_env[idx].reset()]
          new_state[idx] = states[idx][0]
          actions[idx] = []
          rewards[idx] = []

      state = new_state.reshape((num_parallel_rollouts, -1))

    num_completed_rollouts = len(completed_states)
    rollouts = []
    for idx in range(num_completed_rollouts):
      l = len(completed_states[idx])
      rollouts.append({
          'timesteps':
              np.hstack((np.arange(l).reshape((-1, 1)), l * np.ones((l, 1)))),
          'states':
              np.vstack(completed_states[idx]),
          'actions':
              np.vstack(completed_actions[idx]),
          'rewards':
              np.array(completed_rewards[idx]).reshape((-1, 1))
      })
    tf.logging.info(
        'avg rollout length: %f',
        np.mean([rollout['actions'].shape[0] for rollout in rollouts]))
    return rollouts

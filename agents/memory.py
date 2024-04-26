from collections import defaultdict
from typing import Any, Optional, Callable

from beartype import beartype
from einops import rearrange
import numpy as np
from numpy.random import Generator

from helpers.math_util import discount


@beartype
def array_min2d(x: np.ndarray) -> np.ndarray:
    dim_thres = 2
    if x.ndim >= dim_thres:
        return x
    return rearrange(x, "b -> b 1")


class RingBuffer(object):

    @beartype
    def __init__(self, maxlen: int, shape: tuple[int, ...]):
        """Ring buffer implementation"""
        self.maxlen = maxlen
        self.start = 0
        self.length = 0
        self.data = np.zeros((maxlen, *shape), dtype=np.float32)

    @beartype
    def __len__(self):
        return self.length

    @beartype
    def __getitem__(self, idx: int):
        if idx < 0 or idx >= self.length:
            raise KeyError
        return self.data[(self.start + idx) % self.maxlen]

    @beartype
    def get_batch(self, idxs: np.ndarray) -> np.ndarray:
        # important: idxs is a numpy array, and start and maxlen are ints
        return self.data[(self.start + idxs) % self.maxlen]

    @beartype
    def append(self, *, v: np.ndarray):
        if self.length < self.maxlen:
            # we have space, simply increase the length
            self.length += 1
            self.data[(self.start + self.length - 1) % self.maxlen] = v
        elif self.length == self.maxlen:
            # no space, remove the first item
            self.start = (self.start + 1) % self.maxlen
            self.data[(self.start + self.length - 1) % self.maxlen] = v
        else:
            # this should never happen
            raise RuntimeError


class ReplayBuffer(object):

    @beartype
    def __init__(self,
                 np_rng: Generator,
                 capacity: int,
                 erb_shapes: dict[str, tuple[Any, ...]]):
        self.np_rng = np_rng
        self.capacity = capacity
        self.ring_buffers = {n: RingBuffer(self.capacity, s) for n, s in erb_shapes.items()}

    @beartype
    def batchify(self, idxs: np.ndarray) -> dict[str, np.ndarray]:
        """Collect a batch from indices"""
        transitions = {}
        for n in self.ring_buffers:  # avoiding dict comprehension here: introspection
            ll = self.ring_buffers[n].get_batch(idxs)
            aa = array_min2d(ll)
            transitions[n] = aa
        transitions["idxs"] = idxs  # add idxs too
        return transitions

    @beartype
    def sample(self,
               batch_size: int,
               patcher: Optional[Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray]],
        ) -> dict[str, np.ndarray]:
        """Sample transitions uniformly from the replay buffer"""
        idxs = self.np_rng.integers(low=0, high=self.num_entries, size=batch_size)
        transitions = self.batchify(idxs)
        if patcher is not None:
            # patch the rewards
            transitions["rews"] = patcher(
                transitions["obs0"],
                transitions["acs"],
                transitions["obs1"],
            )
        return transitions

    @beartype
    def lookahead(self,
                  transitions: dict[str, np.ndarray],
                  n: int,
                  gamma: float,
                  patcher: Optional[Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray]],
        ) -> dict[str, np.ndarray]:
        """Perform n-step TD lookahead estimations starting from every transition"""
        assert 0 <= gamma <= 1

        # initiate the batch of transition data necessary to perform n-step TD backups
        la_batch = defaultdict(list)

        # iterate over the indices to deploy the n-step backup for each
        for idx in transitions["idxs"]:
            # create indexes of transitions in lookahead of lengths max `n` following sampled one
            la_end_idx = min(idx + n, self.num_entries) - 1
            la_idxs = np.array(range(idx, la_end_idx + 1))
            # collect the batch for the lookahead rollout indices
            la_transitions = self.batchify(la_idxs)
            if patcher is not None:
                # patch the rewards
                la_transitions["rews"] = patcher(
                    la_transitions["obs0"],
                    la_transitions["acs"],
                    la_transitions["obs1"],
                )
            # only keep data from the current episode, drop everything after episode reset, if any
            dones = la_transitions["dones1"]
            term_idx = 1.0
            ep_end_idx = idx + list(dones).index(1.0) if term_idx in dones else la_end_idx
            la_is_trimmed = 0.0 if ep_end_idx == la_end_idx else 1.0
            # compute lookahead length
            td_len = ep_end_idx - idx + 1
            # trim down the lookahead transitions
            la_rews = la_transitions["rews"][:td_len]
            # compute discounted cumulative reward
            la_discounted_sum_n_rews = discount(la_rews, gamma)[0]
            # populate the batch for this n-step TD backup
            la_batch["obs0"].append(la_transitions["obs0"][0])
            la_batch["obs1"].append(la_transitions["obs1"][td_len - 1])
            la_batch["acs"].append(la_transitions["acs"][0])
            la_batch["rews"].append(la_discounted_sum_n_rews)
            la_batch["dones1"].append(la_is_trimmed)
            la_batch["td_len"].append(td_len)

            # add the first next state too: needed in state-only discriminator
            la_batch["obs1_td1"].append(la_transitions["obs1"][0])

            # when dealing with absorbing states
            if "obs0_orig" in la_transitions:
                la_batch["obs0_orig"].append(la_transitions["obs0_orig"][0])
            if "obs1_orig" in la_transitions:
                la_batch["obs1_orig"].append(la_transitions["obs1_orig"][td_len - 1])
            if "acs_orig" in la_transitions:
                la_batch["acs_orig"].append(la_transitions["acs_orig"][0])

        la_batch["idxs"] = [transitions["idxs"]]  # in list for type-checker

        return {k: array_min2d(np.array(v)) for k, v in la_batch.items()}

    @beartype
    def la_sample(self,
                  batch_size: int,
                  n: int,
                  gamma: float,
                  patcher: Optional[Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray]],
        ) -> dict[str, np.ndarray]:
        # sample a batch of transitions
        transitions = self.sample(batch_size, patcher)
        # expand each transition with a n-step TD lookahead
        return self.lookahead(transitions, n, gamma, patcher)

    lookahead_sample = la_sample  # alias for more legibility in agent file

    @beartype
    def append(self, transition: dict[str, np.ndarray]):
        """Add a transition to the replay buffer"""
        assert self.ring_buffers.keys() == transition.keys(), "keys must coincide"
        for k in self.ring_buffers:
            if not isinstance(transition[k], np.ndarray):
                raise TypeError(k)
            self.ring_buffers[k].append(v=transition[k])

    @beartype
    def __repr__(self) -> str:
        return f"ReplayBuffer(capacity={self.capacity})"

    @beartype
    @property
    def latest_entry_idx(self) -> int:
        pick = self.ring_buffers["obs0"]  # could pick any other key
        return (pick.start + pick.length - 1) % pick.maxlen

    @beartype
    @property
    def num_entries(self) -> int:
        return len(self.ring_buffers["obs0"])  # could pick any other key

import gym
from typing import Dict

import ray
from ray.rllib.evaluation.postprocessing import compute_advantages, \
    Postprocessing
from ray.rllib.policy.policy import Policy
from ray.rllib.policy.sample_batch import SampleBatch
from ray.rllib.policy.torch_policy_template import build_torch_policy
from ray.rllib.policy.view_requirement import ViewRequirement
from ray.rllib.utils.framework import try_import_torch

torch, nn = try_import_torch()


def actor_critic_loss(policy, model, dist_class, train_batch):
    logits, _, out = model.from_batch(train_batch)
    dist = dist_class(logits, model)
    log_probs = dist.logp(train_batch[SampleBatch.ACTIONS])
    policy.entropy = dist.entropy().sum()
    policy.pi_err = -train_batch[Postprocessing.ADVANTAGES].dot(
        log_probs.reshape(-1))
    policy.value_err = torch.sum(
        torch.pow(
            out[SampleBatch.VF_PREDS].reshape(-1) - train_batch[Postprocessing.VALUE_TARGETS],
            2.0))
    overall_err = sum([
        policy.pi_err,
        policy.config["vf_loss_coeff"] * policy.value_err,
        -policy.config["entropy_coeff"] * policy.entropy,
    ])
    return overall_err


def loss_and_entropy_stats(policy, train_batch):
    return {
        "policy_entropy": policy.entropy.item(),
        "policy_loss": policy.pi_err.item(),
        "vf_loss": policy.value_err.item(),
    }


def add_advantages(policy,
                   sample_batch,
                   other_agent_batches=None,
                   episode=None):

    completed = sample_batch[SampleBatch.DONES][-1]
    if completed:
        last_r = 0.0
    else:
        last_r = policy._value(sample_batch[SampleBatch.NEXT_OBS][-1])

    return compute_advantages(
        sample_batch, last_r, policy.config["gamma"], policy.config["lambda"],
        policy.config["use_gae"], policy.config["use_critic"])


def apply_grad_clipping(policy, optimizer, loss):
    info = {}
    if policy.config["grad_clip"]:
        for param_group in optimizer.param_groups:
            # Make sure we only pass params with grad != None into torch
            # clip_grad_norm_. Would fail otherwise.
            params = list(
                filter(lambda p: p.grad is not None, param_group["params"]))
            if params:
                grad_gnorm = nn.utils.clip_grad_norm_(
                    params, policy.config["grad_clip"])
                if isinstance(grad_gnorm, torch.Tensor):
                    grad_gnorm = grad_gnorm.cpu().numpy()
                info["grad_gnorm"] = grad_gnorm
    return info


def torch_optimizer(policy, config):
    return torch.optim.Adam(policy.model.parameters(), lr=config["lr"])


class ValueNetworkMixin:
    def _value(self, obs):
        _, _, out = self.model(
            {SampleBatch.OBS: torch.Tensor([obs]).to(self.device)}, [], [1])
        return out[SampleBatch.VF_PREDS][0]


def view_requirements_fn(policy: Policy) -> Dict[str, ViewRequirement]:
    """Function defining the view requirements for training/postprocessing.

    These go on top of the Policy's Model's own view requirements used for
    the action computing forward passes.

    Args:
        policy (Policy): The Policy that requires the returned
            ViewRequirements.

    Returns:
        Dict[str, ViewRequirement]: The Policy's view requirements.
    """
    ret = {
        # Next obs are needed for PPO postprocessing, but not in loss.
        SampleBatch.NEXT_OBS: ViewRequirement(
            SampleBatch.OBS, shift=1, used_for_training=False),
        # Created during postprocessing.
        Postprocessing.ADVANTAGES: ViewRequirement(shift=0),
        Postprocessing.VALUE_TARGETS: ViewRequirement(shift=0),
        # Needed for PPO's loss function.
        SampleBatch.ACTION_DIST_INPUTS: ViewRequirement(shift=0),
        SampleBatch.ACTION_LOGP: ViewRequirement(shift=0),
        SampleBatch.VF_PREDS: ViewRequirement(shift=0),
    }
    # If policy is recurrent, have to add state_out for PPO postprocessing
    # (calculating GAE from next-obs and last state-out).
    if policy.is_recurrent():
        init_state = policy.get_initial_state()
        for i, s in enumerate(init_state):
            ret["state_out_{}".format(i)] = ViewRequirement(
                space=gym.spaces.Box(-1.0, 1.0, shape=(s.shape[0], )),
                used_for_training=False)
    return ret


A3CTorchPolicy = build_torch_policy(
    name="A3CTorchPolicy",
    get_default_config=lambda: ray.rllib.agents.a3c.a3c.DEFAULT_CONFIG,
    loss_fn=actor_critic_loss,
    stats_fn=loss_and_entropy_stats,
    postprocess_fn=add_advantages,
    extra_grad_process_fn=apply_grad_clipping,
    optimizer_fn=torch_optimizer,
    mixins=[ValueNetworkMixin],
    view_requirements_fn=view_requirements_fn,
)

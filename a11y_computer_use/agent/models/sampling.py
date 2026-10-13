"""Temperature and seed for one model call.

Defaults are temperature 0 and seed 0. A provider that has no such parameter
omits it from the request. The trace records the value that was sent, or
``None`` plus a note when it was not sent.

Checked against the Codex CLI on 2026-10-12 (``openai/codex`` ``main``):
``codex-rs/cli/src/main.rs`` has no temperature or seed flag, and
``codex-rs/config/src/config_toml.rs`` ``ConfigToml`` denies unknown fields
and has neither key. ``--config`` cannot carry them. The command provider
does not invent those flags.
"""

from __future__ import annotations

from dataclasses import dataclass

from a11y_computer_use.agent.models.base import ModelError

__all__ = [
    "DEFAULT_SEED",
    "DEFAULT_TEMPERATURE",
    "Sampling",
    "parse_seed",
    "parse_temperature",
    "resolve_sampling",
    "sampling_trace",
]

DEFAULT_TEMPERATURE = 0.0
DEFAULT_SEED = 0

_CODEX_TEMPERATURE_GAP = (
    "the Codex CLI has no temperature flag and config.toml has no temperature key"
)
_CODEX_SEED_GAP = "the Codex CLI has no seed flag and config.toml has no seed key"


@dataclass(frozen=True, slots=True)
class Sampling:
    """What one provider actually sent.

    ``temperature`` and ``seed`` are the values placed on the request.
    ``None`` means that field was left off. ``notes`` says why a requested
    value was left off. An empty ``notes`` means both fields were sent.
    """

    temperature: float | None
    seed: int | None
    notes: tuple[str, ...] = ()

    def trace(self) -> dict:
        """The object stored on a trajectory row."""
        body: dict = {"temperature": self.temperature, "seed": self.seed}
        if self.notes:
            body["notes"] = list(self.notes)
        return body

    def request_fields(self) -> dict:
        """JSON fields to merge into a provider request. Empty when neither is sent."""
        fields: dict = {}
        if self.temperature is not None:
            fields["temperature"] = self.temperature
        if self.seed is not None:
            fields["seed"] = self.seed
        return fields


def parse_temperature(value: object) -> float:
    """A sampling temperature. Rejects bools, non-numbers, and negatives."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelError(f"temperature must be a number >= 0, got {value!r}")
    number = float(value)
    if number < 0 or number != number:
        raise ModelError(f"temperature must be a number >= 0, got {value!r}")
    return number


def parse_seed(value: object) -> int:
    """An integer sampling seed. Integral floats are accepted (JSON ``1.0``)."""
    if isinstance(value, bool):
        raise ModelError(f"seed must be an integer, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise ModelError(f"seed must be an integer, got {value!r}")


def _format_temperature(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return format(value, "g")


def resolve_sampling(
    *,
    temperature: float | None,
    seed: int | None,
    supports_temperature: bool,
    supports_seed: bool,
    temperature_gap: str,
    seed_gap: str,
) -> Sampling:
    """Apply defaults, then drop a field the provider cannot send.

    ``temperature`` or ``seed`` of ``None`` means the default (0 and 0).
    A gap string is the reason recorded when that field is not sent.
    """
    requested_temperature = DEFAULT_TEMPERATURE if temperature is None else parse_temperature(temperature)
    requested_seed = DEFAULT_SEED if seed is None else parse_seed(seed)
    applied_temperature: float | None = requested_temperature
    applied_seed: int | None = requested_seed
    notes: list[str] = []
    if not supports_temperature:
        applied_temperature = None
        notes.append(
            f"temperature {_format_temperature(requested_temperature)} was not sent: {temperature_gap}"
        )
    if not supports_seed:
        applied_seed = None
        notes.append(f"seed {requested_seed} was not sent: {seed_gap}")
    return Sampling(applied_temperature, applied_seed, tuple(notes))


def sampling_trace(model: object) -> dict:
    """Sampling record for a trajectory row.

    A model that does not carry a `Sampling` (a test double, an older custom
    model) is recorded as unsupported. Nothing is raised.
    """
    sampling = getattr(model, "sampling", None)
    if isinstance(sampling, Sampling):
        return sampling.trace()
    name = getattr(model, "name", None)
    label = name if isinstance(name, str) and name else "this model"
    gap = f"{label} does not report sampling support"
    return resolve_sampling(
        temperature=None,
        seed=None,
        supports_temperature=False,
        supports_seed=False,
        temperature_gap=gap,
        seed_gap=gap,
    ).trace()


def codex_sampling(*, temperature: float | None, seed: int | None) -> Sampling:
    """Sampling for the Codex CLI: neither parameter exists, so neither is sent."""
    return resolve_sampling(
        temperature=temperature,
        seed=seed,
        supports_temperature=False,
        supports_seed=False,
        temperature_gap=_CODEX_TEMPERATURE_GAP,
        seed_gap=_CODEX_SEED_GAP,
    )

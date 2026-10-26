"""Explicit bounded policy for uncalibrated operator sessions; no target inspection."""
from pathlib import Path
import math
import yaml

SAFETY_FIELDS = (
    'max_observation_age_seconds', 'max_sample_age_seconds',
    'max_validation_to_dispatch_seconds', 'global_diff_threshold',
    'target_diff_threshold', 'double_click_interval_seconds',
)
OPERATOR_LIMIT_CAPS = {
    'max_observation_age_seconds': 180.0,
    'max_sample_age_seconds': 2.0,
    'max_validation_to_dispatch_seconds': .25,
    'global_diff_threshold': .02,
    'target_diff_threshold': .02,
    'double_click_interval_seconds': .5,
}
OPERATOR_TIMEOUT_CAPS = {
    'transport_timeout_seconds': 15.0,
    'authorization_timeout_seconds': 120.0,
}
OPERATOR_CONFIG_PATH = Path(__file__).resolve().parents[2] / 'config' / 'computer-use-operator-session.yaml'
DEFAULT_HELPER = Path(__file__).resolve().parents[2] / 'bin' / 'linux-aarch64' / 'computer-use-helper'


def load_operator_preset(path: Path) -> tuple[dict, dict]:
    """Read only the eight bounded operator session values from an explicit YAML file."""
    path = Path(path).expanduser()
    try:
        with path.open() as stream:
            values = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f'cannot read operator session safety config: {path}') from exc
    if not isinstance(values, dict) or set(values) != set(SAFETY_FIELDS) | set(OPERATOR_TIMEOUT_CAPS):
        raise ValueError('operator session safety config requires exactly eight known fields')
    for name, cap in {**OPERATOR_LIMIT_CAPS, **OPERATOR_TIMEOUT_CAPS}.items():
        value = values[name]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 < value <= cap):
            raise ValueError(f'{name} exceeds the operator session cap or is invalid')
    return ({key: values[key] for key in SAFETY_FIELDS},
            {key: values[key] for key in OPERATOR_TIMEOUT_CAPS})

"""Constants for the APsystems Modbus integration."""

from typing import TypedDict

DOMAIN = "apsystems_modbus"

CONF_UNIT_IDS = "unit_ids"
CONF_INVERTERS = "inverters"
CONF_ECU_ID = "ecu_id"
CONF_LIFETIME_ENERGY = "lifetime_energy"
CONF_PEAK_POWER = "peak_power"
CONF_INSTALLATION_POWER = "installation_power"
"""Output of all inverters together, in kW; optional."""

CONF_UNIT_ID = "unit_id"
CONF_SERIAL_NUMBER = "serial_number"
CONF_MODEL = "model"


class InverterConfig(TypedDict):
    """One inverter as stored in the config entry."""

    unit_id: int
    serial_number: str
    model: str


DEFAULT_PORT = 502

ROUND_PERIOD = 300
"""Seconds between two ECU rounds. A round starts about 10 s past every fifth
minute of the clock and refreshes the inverters one by one."""
SETTLE_BASE = 30
SETTLE_PER_INVERTER = 4
SETTLE_MAX = 280
"""Seconds into a round before polling: time for the ECU to refresh the
inverters, longer with more of them, and always before the next round."""
MIN_POLL_GAP = 60
"""Shortest wait between two polls, also when a round is close."""

SLOW_POLL_PER_UNIT = 5
"""Seconds per inverter read above which a poll counts as slow (healthy: 2-4 s)."""

POLL_BUDGET = 150
"""Seconds into a poll after which no further inverter is started, so a poll
of a slow ECU normally ends before its next round."""
VALUE_MAX_AGE = 900
"""Seconds a read value keeps counting when later polls miss that inverter:
three ECU rounds."""
MAX_INVERTER_POWER = 5000
"""Watts no microinverter reaches; a higher reading is not counted in totals."""
MIN_POWER_PER_INVERTER = 0.2
MAX_POWER_PER_INVERTER = 2.5
"""Plausible output of one microinverter in kW, to check the installation power."""

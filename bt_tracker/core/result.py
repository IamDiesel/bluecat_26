from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TrackingResult:
    """Standardisiertes Rückgabe-Objekt der Tracking-Engines nach einem Tick."""

    state: str  # "aktiv" oder "inaktiv"
    x_cm: Optional[float]
    y_cm: Optional[float]
    accuracy_cm: Optional[float]
    active_sensors_count: int
    contributing_sensors: List[str]
    rejected_sensors: List[str]
    inactive_sensors: List[str]
    sensor_measurements: List[Dict[str, Any]]
    radio_diagnostics: dict
    estimate_rejected: bool = False
    rejection_reason: str = ""
    # Erweiterungen (neues Modell; im Legacy-Modell teilweise leer)
    room: Optional[str] = None
    room_probabilities: Dict[str, float] = field(default_factory=dict)
    moving_probability: Optional[float] = None
    engine: str = ""
    updated: bool = False  # True, wenn in diesem Tick neue Messungen eingingen

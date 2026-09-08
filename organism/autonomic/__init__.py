"""Explicit, overlapping autonomic branch hierarchy.

The branch names are software metaphors and do not assert literal physiology.
"""

from .base import AutonomicBranch, AutonomicContext, RecruitmentStrategy
from .coordinator import (
    BRANCHES,
    DEFAULT_AUTONOMIC_CALIBRATION,
    AutonomicCalibration,
    branch_tendencies,
)
from .dorsal_vagal import DORSAL_VAGAL, DorsalVagalBranch
from .parasympathetic import PARASYMPATHETIC, ParasympatheticBranch
from .sympathetic import SYMPATHETIC, SympatheticBranch
from .ventral_vagal import VENTRAL_VAGAL, VentralVagalBranch

__all__ = [
    "AutonomicBranch",
    "AutonomicCalibration",
    "AutonomicContext",
    "BRANCHES",
    "DEFAULT_AUTONOMIC_CALIBRATION",
    "DORSAL_VAGAL",
    "DorsalVagalBranch",
    "PARASYMPATHETIC",
    "ParasympatheticBranch",
    "RecruitmentStrategy",
    "SYMPATHETIC",
    "SympatheticBranch",
    "VENTRAL_VAGAL",
    "VentralVagalBranch",
    "branch_tendencies",
]

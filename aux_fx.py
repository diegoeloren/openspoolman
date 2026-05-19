"""
This file covers auxilary function used accross the project
"""

from datetime import datetime
from config import TZINFO


def now():
    return datetime.now(TZINFO)
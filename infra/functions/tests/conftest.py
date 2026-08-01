"""The backend modules import each other flatly (`import structure as st`), the
way the Functions host loads them, so the function app's directory has to be on
sys.path for the tests to import them the same way."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

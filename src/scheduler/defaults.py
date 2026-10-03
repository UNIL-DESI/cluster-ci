"""
defaults.py - Re-export pur de src.config.defaults (source unique de vérité v3).
Aucune duplication de constante.
"""

try:
    from src.config.defaults import *
except ImportError:
    from config.defaults import *

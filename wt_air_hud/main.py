"""
War Thunder Air Battle HUD - Main Entry Point

Usage:
    python main.py

Requirements:
    - Game in Windowed or Borderless Windowed mode
    - PyQt5 installed
    - 8111 port active (game running)
"""
import sys
import os

# Add project dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hud_overlay import main

if __name__ == "__main__":
    main()

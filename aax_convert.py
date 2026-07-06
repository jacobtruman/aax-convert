#!/usr/bin/env python3
# vim: tabstop=4:softtabstop=4:shiftwidth=4:expandtab:
# -*- coding: utf-8 -*-
"""Backward-compatible wrapper for the aax-convert package.

This file exists for backward compatibility. The actual implementation
has been refactored into the aax_convert/ package.

Usage: python aax_convert.py [options] <input.aax>
"""

from aax_convert.cli import main

if __name__ == "__main__":
    main()

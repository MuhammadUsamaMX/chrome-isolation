#!/bin/bash

# Chrome Isolation Manager - Startup Script
# This script ensures X11 access is configured on login
# Add this to your startup applications

# Setup X11 access for Docker
if command -v xhost &> /dev/null; then
    xhost +local:docker > /dev/null 2>&1
    echo "Chrome Isolation Manager: X11 access configured"
fi

# Ensure service is running
if command -v systemctl &> /dev/null; then
    if ! systemctl --user is-active --quiet chrome-manager.service 2>/dev/null; then
        if systemctl is-active --quiet chrome-manager.service 2>/dev/null; then
            echo "Chrome Isolation Manager: Service is running"
        fi
    fi
fi

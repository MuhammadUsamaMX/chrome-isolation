#!/bin/bash

# Chrome Launcher Script
# This script is called by desktop entries to launch isolated Chrome profiles

PROFILE_NAME="$1"

if [ -z "$PROFILE_NAME" ]; then
    echo "Usage: $0 [ProfileName]"
    exit 1
fi

# API endpoint
API_URL="http://127.0.0.1:5000/api/profiles/${PROFILE_NAME}/start"

# Start the container via API
curl -s -X POST "$API_URL" > /dev/null 2>&1

# Give it a moment to start
sleep 1

exit 0

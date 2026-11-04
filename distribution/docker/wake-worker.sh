#!/bin/sh
# The shared worker uses the same five-minute wake as the Linux operator timer.
python -I -B -m scope_recall.runtime.resume_entry --config /data/store/runtime-config.json
sleep 300

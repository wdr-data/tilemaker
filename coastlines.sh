#!/bin/bash

./tilemaker \
    --output ./tilesets/coastline.mbtiles \
    --bbox -180,-85,180,85 \
    --config resources/config-coastline.json \
    --process resources/process-coastline.lua

#!/bin/bash

./tilemaker \
    --input "./osm/dach-260204-renumbered.osm.pbf" \
    --output ./tilesets/dach.mbtiles \
    --config ./resources/config-openmaptiles-dach.json \
    --process ./resources/process-openmaptiles.lua \
    --compact  # requires `osmium renumber` to have been run on the input file

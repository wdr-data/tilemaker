#!/bin/bash

./tilemaker \
    --input "./osm/europe-latest-renumbered.osm.pbf" \
    --output ./tilesets/europe.mbtiles \
    --config ./resources/config-openmaptiles-europe.json \
    --process ./resources/process-openmaptiles.lua \
    --compact  # requires `osmium renumber` to have been run on the input file

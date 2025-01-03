#!/bin/bash

./tilemaker \
    --input "./osm/europe-latest-renumbered.osm.pbf" \
    --output ./tilesets/nrw.mbtiles \
    --config ./resources/config-openmaptiles-nrw.json \
    --process ./resources/process-openmaptiles.lua \
    --bbox 4.21,48.51,11.77,53.44 \
    --compact  # requires `osmium renumber` to have been run on the input file

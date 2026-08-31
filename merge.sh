#!/bin/bash
# Some tiles are larger than the default 500kb limit for tile-join.
./../tippecanoe/tile-join \
    --no-tile-size-limit \
    -o ./tilesets/nrw-v3.mbtiles \
    ./tilesets/coastline.mbtiles \
    ./tilesets/europe.mbtiles \
    ./tilesets/dach.mbtiles \
    ./tilesets/nrw.mbtiles

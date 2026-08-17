neo4j-admin database import full neo4j \
--nodes=Artist=artists.csv \
--nodes=Master=masters.csv \
--relationships=RELEASED=master_artist_links.csv \
--skip-bad-relationships=true --multiline-fields=true --bad-tolerance=100000 --overwrite-destination

#!/usr/bin/env python3

import os
import csv
import gzip
import time
import traceback
import click
import sqlite3

from progress import Heartbeat, format_duration


from lxml.etree import Element
import lxml.etree as et


# Discogs sentinel artist ids: placeholders with no artist page of their own,
# credited on releases instead of a real performer. 194 is "Various" (a
# compilation credited as a whole), 355 is "Unknown Artist". Neither exists in
# the artists dump -- confirmed absent, not merely dropped by a parsing bug --
# so every CREDITED row naming them would otherwise dangle. Writing a generic
# node for them instead would be worse than dropping the edge: it would link
# together thousands of releases that share nothing but an unknown performer.
PLACEHOLDER_ARTIST_IDS = {"194", "355"}


def open_writer( fn, header, write_header=True ):
    writer = csv.writer( open(fn,"a"), quoting=csv.QUOTE_STRINGS )
    if write_header and (header is not None):
        writer.writerow( header )
    return writer


def init_csv_writers( out_dir, xml_files, write_releases=False ):
    """
    Create a csv.writer per output file, with the header row Neo4j's bulk
    importer expects. Note that files are opened in append mode, so the output
    folder must be empty -- see main().
    """
    
    writers = {}
    
    if xml_files["artist"] is not None:

        # Artist and Group are separate Neo4j labels but share one import ID
        # space, "Entity". They have to: Discogs issues artist and group IDs
        # from a single sequence, and a release credits a band by exactly the
        # same kind of ID it uses for a person. With separate spaces every
        # release credited to a band produced a dangling CREDITED row, because
        # the band's ID lives in groups.csv while the edge declared
        # :START_ID(Artist). A shared space stays unique because each Discogs
        # ID lands in exactly one of the two files.
        writers["artists"] = open_writer(
            f"{out_dir}/artists.csv",
            ["artistId:ID(Entity)", "name", "realname", "profile"]
        )

        writers["groups"] = open_writer(
            f"{out_dir}/groups.csv",
            ["groupId:ID(Entity)", "name", "profile"]
        )

        writers["artist_group_links"] = open_writer(
            f"{out_dir}/artist_group_links.csv",
            [":START_ID(Entity)", ":END_ID(Entity)"]
        )


    # Masters are an intermediate hop, not a node: they are resolved to their
    # main_release and written out as Release rows. See process_master().

    if write_releases:

        writers["releases"] = open_writer(
            f"{out_dir}/releases.csv",
            ["releaseId:ID(Release)", "year", "title"]
        )

        # Entity, not Artist: the credited party may be a band. See above.
        writers["artist_release_links"] = open_writer(
            f"{out_dir}/artist_release_links.csv",
            [":START_ID(Entity)", ":END_ID(Release)"]
        )

        writers["release_label_links"] = open_writer(
            f"{out_dir}/release_label_links.csv",
            [":START_ID(Release)", ":END_ID(Label)"]
        )


    if xml_files["label"] is not None:

        writers["labels"] = open_writer(
            f"{out_dir}/labels.csv",
            ["labelId:ID(Label)", "name", "profile"]
        )

        writers["label_sublabel_links"] = open_writer(
            f"{out_dir}/label_sublabel_links.csv",
            [":START_ID(Label)", ":END_ID(Label)"]
        )

    
    return writers


def init_sqlite_files( sqlite_files ):

    if sqlite_files["label"] is not None:
        conn = sqlite3.connect( sqlite_files["label"] )
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS labels (
                id INTEGER PRIMARY KEY,
                name TEXT,
                profile TEXT
            )
        ''')
        conn.commit()
        sqlite_files["label"] = cursor


    if sqlite_files["release"] is not None:
        conn = sqlite3.connect( sqlite_files["release"] )
        sqlite_files["release"] = conn.cursor()



def safe_text( element, tag_name ):
    return element.find(tag_name).text if element.find(tag_name) is not None else ""


def upsert_label( sqlite_files, writers, label_id, name, profile="" ):
    """
    Record a Label. The upsert keeps whichever sighting is richer, which is why
    labels stage through SQLite rather than being written straight to CSV.
    """
    if not label_id:
        return
    if sqlite_files["label"]:
        sqlite_files["label"].execute(
            "INSERT INTO labels (id, name, profile) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "profile=COALESCE(NULLIF(excluded.profile,''), profile)",
            ( label_id, name or "", profile )
        )
    else:
        writers["labels"].writerow([ label_id, name or "", profile ])


def process_label( element: Element, writers, xml_files, sqlite_files ):

    p = element.getparent()

    if p is not None and p.tag == "sublabels":
        # <label><id>1</id>...<sublabels><label id="2">Name</label></sublabels>
        # The dump contains malformed entries -- a bare <label/> with no id --
        # so read defensively rather than raising past the caller.
        element_id = element.attrib.get("id")
        parent_id = p.getparent().find("id") if p.getparent() is not None else None
        if element_id is None or parent_id is None:
            return

        writers["label_sublabel_links"].writerow([ element_id, parent_id.text ])

        # Register the sublabel as a Label in its own right. Leaving this out
        # was the original behaviour, on the theory that every sublabel also
        # appears as a top-level <label>; where that does not hold the SUBLABEL
        # edge dangles and the import rejects it. Going through the upsert makes
        # it safe either way -- a later top-level record supersedes this
        # name-only one, and re-seeing the same id is a no-op.
        upsert_label( sqlite_files, writers, element_id, element.text )
        return

    if element.xpath('ancestor::release'):
        # A label referenced inside a release listing, which carries only
        # id and name as attributes.
        upsert_label( sqlite_files, writers,
                      element.attrib.get("id"), element.attrib.get("name") )
        return

    # A top-level <label>. Some have no <name>; take what is there rather than
    # dropping the record, since its id is still referenced by other rows.
    element_id = element.find("id")
    if element_id is None:
        return
    upsert_label( sqlite_files, writers, element_id.text,
                  safe_text( element, "name" ), safe_text( element, "profile" ) )


def process_artist(element: Element, writers):

    try:

        # A handful of artists have no <name>. Keep the record anyway: its id is
        # referenced by MEMBER_OF and CREDITED rows, and dropping the node makes
        # every one of those dangle.
        element_id = element.find("id").text
        element_name = safe_text( element, "name" )
        element_profile = safe_text( element, "profile" )

        members = element.find("members")
        if members is not None:

            # this is actually a group; element_id is the group, and each
            # <members><name id="..."> is a member artist. The relationship is
            # MEMBER_OF, so the artist is the START and the group is the END.
            for name in members.findall("name"):
                writers["artist_group_links"].writerow([
                    name.attrib["id"],
                    element_id
                ])

            writers["groups"].writerow([
                element_id,
                element_name,
                element_profile
            ])

        else:

            writers["artists"].writerow([
                element_id,
                element_name,
                safe_text( element, "realname" ),
                element_profile
            ])
    
    except Exception as e:
        traceback.print_exc()
        print( et.tostring(element))


def process_release( element: Element, writers, xml_files, sqlite_files ):

    release_id = element.attrib["id"]

    try:

        writers["releases"].writerow([
            release_id,
            safe_text( element, "released" ),
            element.find("title").text
        ])

        artists = element.find("artists")
        extra_artists = element.find("extraartists")
        for source in [ artists, extra_artists ]:
            if source is not None:
                for artist in source.findall("artist"):
                    artist_id = artist.find("id")
                    if artist_id is not None and artist_id.text not in PLACEHOLDER_ARTIST_IDS:
                        writers["artist_release_links"].writerow([
                            artist_id.text,
                            release_id
                        ])
        
        labels = element.find("labels")
        if labels is not None:
            for label_elem in labels.findall("label"): 
                label_id = label_elem.attrib.get("id")
                if label_id is not None:
                    process_label( label_elem, writers, xml_files, sqlite_files )
                    writers["release_label_links"].writerow([
                        release_id,
                        label_id
                    ])
    
    except Exception as e:
        traceback.print_exc()
        print( et.tostring(element) )
    

def fetch_element_sqlite( sqlite_cursor, table, element_id ):
    res = sqlite_cursor.execute(
        f"SELECT content FROM {table} WHERE id = ?",
        (element_id,)
    )
    return et.fromstring(
        res.fetchone()[0]
    )


def process_master( element: Element, writers, xml_files, sqlite_files ):
    """
    Masters are not written out as nodes. We are less interested in masters than
    in releases, so each master is resolved to its main_release -- the canonical
    pressing rather than every reissue -- and that release is written instead.
    """

    try:
        release_id = int( element.find("main_release").text )

        elem_release = fetch_element_sqlite(
            sqlite_files["release"], "releases", release_id
        )
        process_release( elem_release, writers, xml_files, sqlite_files )

    except Exception as e:
        print( repr(e) )



def open_dump( fn ):
    """
    Open a Discogs dump for streaming, transparently handling gzip.

    Returns (parse_fp, progress_fp, total_bytes). Progress is measured against
    the *compressed* file: progress_fp is the raw handle, whose position
    advances as the gzip layer consumes it, so percentages stay meaningful
    without ever decompressing to disk.
    """
    raw = open( fn, "rb" )
    if fn.endswith(".gz"):
        return gzip.open( raw, "rb" ), raw, os.path.getsize(fn)
    return raw, raw, os.path.getsize(fn)


def stream_elements( fn_xml, tag, on_element ):
    """
    Stream <tag> elements from an XML dump, calling on_element(element) for each.

    Only matching elements are reported (iterparse tag filter), and each is
    pruned along with its already-processed previous siblings once handled, so
    peak memory stays flat regardless of file size. Without that pruning the
    cleared elements accumulate under the root as empty nodes, which costs over
    a gigabyte across a full releases dump.
    """
    parse_fp, progress_fp, total_bytes = open_dump( fn_xml )
    num_errors = 0

    # Progress is measured in bytes of the file consumed, so it stays accurate
    # regardless of how many records a dump happens to hold.
    beat = Heartbeat( tag, total=total_bytes, unit=f"{tag}s" )
    beat.begin( f"{os.path.basename(fn_xml)} ({total_bytes/2**30:.2f} GiB)" )

    try:
        context = et.iterparse( parse_fp, events=("end",), tag=tag )

        for event, element in context:

            try:
                on_element( element )
            except AttributeError as e:
                print( et.tostring(element) )
                print( repr(e) )
                num_errors += 1

            parent = element.getparent()
            element.clear()
            if parent is not None:
                while element.getprevious() is not None:
                    del parent[0]

            beat.tick( position=progress_fp.tell() )
    finally:
        parse_fp.close()
        if progress_fp is not parse_fp:
            progress_fp.close()

    num_records = beat.count
    error_rate = (num_errors / num_records * 100) if num_records else 0.0
    beat.finish( f"{num_errors:,} errors ({error_rate:.4f}%)" )
    return num_records


def collect_main_release_ids( fn_master_xml ):
    """
    First pass of the prefilter: read the masters dump and collect the set of
    main_release IDs.

    Only these releases are ever imported -- roughly 3M of the ~18M in the
    releases dump -- so knowing the set up front means the releases dump can be
    streamed once, straight from its .gz, instead of being split, indexed into
    SQLite and randomly accessed.
    """
    wanted = set()

    def on_master( element ):
        main_release = element.find("main_release")
        if main_release is not None and main_release.text:
            wanted.add( int(main_release.text) )

    print(f"Collecting main_release IDs from {fn_master_xml}")
    stream_elements( fn_master_xml, "master", on_master )
    print(f"  {len(wanted):,} distinct main_release IDs")
    return wanted


def parse_releases_filtered( fn_release_xml, wanted_ids, writers, xml_files, sqlite_files ):
    """
    Second pass of the prefilter: stream the releases dump and write out only
    the releases named by wanted_ids.
    """
    stats = { "matched": 0 }

    def on_release( element ):
        try:
            release_id = int( element.attrib["id"] )
        except (KeyError, ValueError):
            return
        if release_id in wanted_ids:
            stats["matched"] += 1
            process_release( element, writers, xml_files, sqlite_files )

    print(f"Streaming {fn_release_xml}, keeping {len(wanted_ids):,} releases")
    stream_elements( fn_release_xml, "release", on_release )
    print(f"  wrote {stats['matched']:,} releases")

    missing = len(wanted_ids) - stats["matched"]
    if missing > 0:
        print(f"  note: {missing:,} main_release IDs were not found in the releases dump")


@click.command()
@click.option('--artist-xml', type=click.Path(exists=True), help='Path to artist XML dump (.xml or .xml.gz)')
@click.option('--master-xml', type=click.Path(exists=True), help='Path to master XML dump (.xml or .xml.gz)')
@click.option('--label-xml', type=click.Path(exists=True), help='Path to label XML dump (.xml or .xml.gz)')
@click.option('--release-xml', type=click.Path(exists=True), help='Path to release XML dump (.xml or .xml.gz). Streamed once, prefiltered to the main_release IDs named by --master-xml. Preferred.')
@click.option('--release-sqlite', type=click.Path(exists=True), help='Path to a prebuilt release SQLite index (see releases_to_sqlite.py). Alternative to --release-xml.')
@click.option('--label-sqlite', type=click.Path(), help='Path to label SQLite file (created if absent). Needed to dedupe labels.')
@click.option('--output-folder', required=True, type=click.Path(), help='Output folder for CSV files (must be empty)')
def main(artist_xml, master_xml, label_xml, release_xml, release_sqlite, label_sqlite, output_folder):

    if release_xml and release_sqlite:
        print( "Specify either --release-xml or --release-sqlite, not both." )
        exit(1)

    if master_xml is not None and (release_xml is None and release_sqlite is None):
        print( "If a masters file is specified, --release-xml or --release-sqlite must also be specified." )
        exit(1)

    if release_xml is not None and master_xml is None:
        print( "--release-xml needs --master-xml: the master dump names which releases to keep." )
        exit(1)

    os.makedirs(output_folder, exist_ok=True)

    # CSVs are opened in append mode, so re-running into a folder that already
    # holds output silently duplicates every row and re-writes headers mid-file,
    # which the bulk importer then rejects. Refuse instead of corrupting.
    existing_csvs = [ f for f in os.listdir(output_folder) if f.endswith(".csv") ]
    if existing_csvs:
        print( f"Output folder {output_folder} already contains CSV files: {', '.join(sorted(existing_csvs))}" )
        print( "Use an empty folder, or move the existing files aside first." )
        exit(1)

    xml_files = {
        'artist': artist_xml,
        'label': label_xml,
        'master': master_xml
    }

    sqlite_files = {
        'release': release_sqlite,
        'label': label_sqlite,
    }

    init_sqlite_files( sqlite_files )

    write_releases = (release_xml is not None) or (sqlite_files["release"] is not None)
    writers = init_csv_writers( output_folder, xml_files, write_releases )

    # Artists and labels are independent single passes. Labels go first so that
    # the richer records from the labels dump land before the bare name-only
    # references picked up from release listings.
    if label_xml:
        print(f"Processing label from {label_xml}")
        stream_elements( label_xml, "label",
            lambda e: process_label( e, writers, xml_files, sqlite_files ) )

    if artist_xml:
        print(f"Processing artist from {artist_xml}")
        stream_elements( artist_xml, "artist",
            lambda e: process_artist( e, writers ) )

    if release_xml:
        # Preferred path: two streaming passes, no index, reads .gz directly.
        wanted_ids = collect_main_release_ids( master_xml )
        parse_releases_filtered(
            release_xml, wanted_ids, writers, xml_files, sqlite_files
        )
    elif master_xml:
        # Index path: one pass over masters, random-access lookup per release.
        print(f"Processing master from {master_xml}")
        stream_elements( master_xml, "master",
            lambda e: process_master( e, writers, xml_files, sqlite_files ) )

    for k in sqlite_files:
        v = sqlite_files[k]
        if v:
            v.connection.commit()

if __name__ == "__main__":
    main()

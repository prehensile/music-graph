#!/usr/bin/env python3

import os
import csv
import time
from alive_progress import alive_bar
import traceback
import click
import sqlite3


from lxml.etree import Element
import lxml.etree as et


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
        
        writers["artists"] = open_writer(
            f"{out_dir}/artists.csv",
            ["artistId:ID(Artist)", "name", "realname", "profile"]
        )

        writers["groups"] = open_writer(
            f"{out_dir}/groups.csv",
            ["groupId:ID(Group)", "name", "profile"]
        )

        writers["artist_group_links"] = open_writer(
            f"{out_dir}/artist_group_links.csv",
            [":START_ID(Artist)", ":END_ID(Group)"]
        )


    # Masters are an intermediate hop, not a node: they are resolved to their
    # main_release and written out as Release rows. See process_master().

    if write_releases:

        writers["releases"] = open_writer(
            f"{out_dir}/releases.csv",
            ["releaseId:ID(Release)", "year", "title"]
        )

        writers["artist_release_links"] = open_writer(
            f"{out_dir}/artist_release_links.csv",
            [":START_ID(Artist)", ":END_ID(Release)"]
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


def process_label( element: Element, writers, xml_files, sqlite_files ):

    p = element.getparent()

    if p.tag == "sublabels":

        # actually a sublabel
        element_id = element.attrib["id"]
        element_name = element.text

        label_id = p.getparent().find("id").text

        writers["label_sublabel_links"].writerow([
            element_id,
            label_id
        ])
        
        # current theory: sublabels are already included in labels, this is introducing duplicates
        # writers["labels"].writerow([
        #     element_id,
        #     element_name,
        #     ""
        # ])
    
    else:

        element_id = None
        element_name = None
        element_profile = ""

        res = element.xpath('ancestor::release')
        if len(res) > 0:
            # node has an ancestor named release, so we're a label node in a release listing
            element_id = element.attrib["id"]
            element_name = element.attrib["name"]
        else:
            # no grandparent named release, probably a regular label node
            element_id = element.find("id").text
            element_name = element.find("name").text
            element_profile = safe_text( element, "profile" )

        if sqlite_files["label"]:
            cursor = sqlite_files["label"]
            cursor.execute(
                "INSERT INTO labels (id, name, profile) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, profile=COALESCE(NULLIF(excluded.profile,''), profile)",
                ( element_id, element_name, element_profile)
            )

        else:

            writers["labels"].writerow([
                element_id,
                element_name,
                element_profile
            ])

def process_artist(element: Element, writers):

    try:

        element_id = element.find("id").text
        element_name = element.find("name").text
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
                    if artist_id is not None:
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



def parse_node( element, writers, xml_files, sqlite_files ):
    node_type = element.tag 
    if node_type == "artist":
        process_artist( element, writers )
    elif node_type == "master":
        process_master( element, writers, xml_files, sqlite_files )
    elif node_type == "label":
        process_label( element, writers, xml_files, sqlite_files )


def parse_xml( fn_xml, node_type, writers, xml_files, sqlite_files ):
    context_size = os.path.getsize(fn_xml)
    start_time = time.time()
    
    with open(fn_xml, "rb") as fp_xml:
        context = et.iterparse(
            fp_xml,
            events=["end"]
        )

        num_records = 0
        num_errors = 0

        with alive_bar(dual_line=True, receipt_text=True) as bar:
            for event, element in context:

                # wait for the end of the tag we're looking for, otherwise we end up trying to parse all its children
                if element.tag == node_type:
                    
                    try:
                        parse_node( element, writers, xml_files, sqlite_files )
                    except AttributeError as e:
                        print( et.tostring(element) )
                        print(repr(e))
                        num_errors += 1
                        # raise
                
                    num_records += 1
                    element.clear()
                    
                current_pos = fp_xml.tell()
                pc = (current_pos / context_size) * 100
                
                # Calculate ETA
                elapsed_time = time.time() - start_time
                if pc > 0 and elapsed_time > 0:
                    bytes_per_second = current_pos / elapsed_time
                    remaining_bytes = context_size - current_pos
                    eta_seconds = remaining_bytes / bytes_per_second
                    
                    # Format ETA
                    if eta_seconds < 60:
                        eta_str = f"{eta_seconds:.0f}s"
                    elif eta_seconds < 3600:
                        eta_str = f"{eta_seconds//60:.0f}m {eta_seconds%60:.0f}s"
                    else:
                        eta_str = f"{eta_seconds//3600:.0f}h {(eta_seconds%3600)//60:.0f}m"
                    
                    bar.text(f"{pc:.2f}% of file (ETA: {eta_str})")
                else:
                    bar.text(f"{pc:.2f}% of file")
                bar() 
                
        
        print(f"Processed {num_records:,} {node_type} with {num_errors:,} errors "
              f"({(num_errors/num_records) * 100}% error rate)")


@click.command()
@click.option('--artist-xml', type=click.Path(exists=True), help='Path to artist XML file')
@click.option('--release-sqlite', type=click.Path(exists=True), help='Path to release SQLite index (built by releases_to_sqlite.py)')
@click.option('--label-xml', type=click.Path(exists=True), help='Path to label XML file')
@click.option('--label-sqlite', type=click.Path(), help='Path to label SQLite file (created if absent)')
@click.option('--master-xml', type=click.Path(exists=True), help='Path to master XML file')
@click.option('--output-folder', required=True, type=click.Path(), help='Output folder for CSV files (must be empty)')
def main(artist_xml, release_sqlite, label_xml, label_sqlite, master_xml, output_folder):

    if master_xml is not None and release_sqlite is None:
        print( "If a masters file is specified, --release-sqlite must also be specified." )
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

    write_releases = sqlite_files["release"] is not None
    writers = init_csv_writers( output_folder, xml_files, write_releases )

    for node_type, xml_file in xml_files.items():
        if xml_file:
            print(f"Processing {node_type} from {xml_file}")
            parse_xml( xml_file, node_type, writers, xml_files, sqlite_files )

    for k in sqlite_files:
        v = sqlite_files[k]
        if v:
            v.connection.commit()

if __name__ == "__main__":
    main()

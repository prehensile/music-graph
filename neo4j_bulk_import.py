import sys
import os
import csv
from xml.etree import ElementTree
from xml.etree.ElementTree import Element
from alive_progress import alive_bar


def process_master( element:Element, csv_writer_masters, csv_writer_master_artist_links ):
    
    master_id =  element.attrib["id"]
    
    csv_writer_masters.writerow([
        master_id,
        element.find("year").text,
        element.find("title").text
    ])

    for artist_elem in element.findall(".//artist"):
        
        artist_id = artist_elem.find("id").text
        
        #skip "Various" artists, fucks things up
        if artist_id == "194":
            continue
        
        csv_writer_master_artist_links.writerow([
            artist_id,
            master_id
        ])

def parse_masters( fn_xml ):

    context_size = os.path.getsize( fn_xml )

    fp_masters = open( "masters.csv", "w" )
    csv_writer_masters = csv.writer( fp_masters )
    csv_writer_masters.writerow(
        ["masterId:ID(Master)", "year", "title"]
    )

    fp_master_artist_links = open( "master_artist_links.csv", "w" )
    csv_master_artist_links= csv.writer( fp_master_artist_links )
    csv_master_artist_links.writerow(
        [":START_ID(Artist)",":END_ID(Master)"]
    )

    with open( fn_xml, "rb") as fp_xml:
        
        context = ElementTree.iterparse(
            fp_xml,
            events=["end"]
        )

        num_masters = 0
        num_errors = 0

        with alive_bar( dual_line=True, receipt_text=True ) as bar:
            for event, element in context:
                if element.tag == "master":
                    
                    try:
                        process_master( element, csv_writer_masters, csv_master_artist_links )                
                    except AttributeError as e:
                        print( repr(e) )
                        num_errors +=1
                        pass
                    
                    num_masters += 1

                    pc = (fp_xml.tell() / context_size) * 100
                    bar.text( f"{pc:.2f}% of file" )
                    
                    bar()
                    element.clear()
        
        print( f"Processed {num_masters:,} masters with {num_errors:,} errors ( { (num_errors/num_masters) * 100 }% error rate )" )


def process_artist( element:Element, csv_writer_artists ):
    
    csv_writer_artists.writerow([
        element.find("id").text,
        element.find("name").text,
        element.find("realname").text if element.find("realname") is not None else "",
        element.find("profile").text if element.find("profile") is not None else ""
    ])



def parse_artists( fn_xml ):

    context_size = os.path.getsize( fn_xml )

    fp_artists = open( "artists.csv", "w" )
    csv_writer_artists = csv.writer( fp_artists, quoting=csv.QUOTE_STRINGS )
    csv_writer_artists.writerow(
        ["artistId:ID(Artist)", "name", "realname", "profile" ]
    )

    with open( fn_xml, "rb") as fp_xml:
        
        context = ElementTree.iterparse(
            fp_xml,
            events=["end"]
        )

        num_artists = 0
        num_errors = 0

        with alive_bar( dual_line=True, receipt_text=True ) as bar:
            for event, element in context:
                if element.tag == "artist":
                    
                    try:
                        process_artist( element, csv_writer_artists )                
                    except AttributeError as e:
                        print( repr(e) )
                        num_errors +=1
                    
                    num_artists += 1

                    pc = (fp_xml.tell() / context_size) * 100
                    bar.text( f"{pc:.2f}% of file" )
                    
                    bar()
                    element.clear()
        
        print( f"Processed {num_artists:,} artists with {num_errors:,} errors ( { (num_errors/num_artists) * 100 }% error rate )" )


fn_xml = sys.argv[1]
node_type = sys.argv[2]

if node_type == "artists":
    parse_artists( fn_xml )

elif node_type == "masters":
    parse_masters( fn_xml )
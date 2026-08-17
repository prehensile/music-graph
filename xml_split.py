from lxml.etree import ElementTree, Element, iterparse
import lxml.etree as et

import os, sys

fn_xml = sys.argv[1]
tag_name = sys.argv[2]
num_chunks = int(sys.argv[3])
root_tag = sys.argv[4]

xml_size = os.path.getsize( fn_xml )

chunk_size = int( xml_size / num_chunks )

offset = 0

tag_close = f"</{tag_name}>".encode()

def find_next_tag_end(fp, tag_close, start_pos):
    """Find the next occurrence of tag_close starting from start_pos"""
    fp.seek(start_pos)
    buf = b""
    lc = len(tag_close)
    
    while True:
        byte = fp.read(1)
        if not byte:  # EOF
            return None
        buf += byte
        if buf[-lc:] == tag_close:
            return fp.tell()

offsets = [0]

with open(fn_xml, "rb") as fp_xml:
    current_pos = 0
    
    while current_pos < xml_size:
        # Find the next </release> tag from current position
        tag_end = find_next_tag_end(fp_xml, tag_close, current_pos)
        if tag_end is None:
            break
        
        # Move forward approximately chunk_size and find next tag boundary
        target_pos = tag_end + chunk_size
        if target_pos >= xml_size:
            # Last chunk goes to end of file
            offsets.append(xml_size)
            break
        
        # Find the next </release> tag after target position
        next_split = find_next_tag_end(fp_xml, tag_close, target_pos)
        if next_split is None:
            # No more tags, last chunk goes to end
            offsets.append(xml_size)
            break
        
        offsets.append(next_split)
        current_pos = next_split

print( offsets )

fp_xml.close()


def extract_section( input_file, output_file, start, size, head=None, tail=None ):
    
    """Extract a chunk from input file using fast Python streaming"""
    
    with open(input_file, 'rb') as infile, open(output_file, 'wb') as outfile:

        if head is not None:
            outfile.write( head )

        infile.seek(start)
        remaining = size
        chunk_size = 64 * 1024  # 64KB chunks for optimal performance
        
        while remaining > 0:
            to_read = min(chunk_size, remaining)
            data = infile.read(to_read)
            if not data:
                break
            outfile.write(data)
            remaining -= len(data)
        
        if tail is not None:
            outfile.write( tail )


for i, offset in enumerate(offsets):

    if i==0:
        continue

    fn_out = f"{fn_xml}.{i}"
    prev_offset = offsets[i-1]
    count = offset - prev_offset
    
    head = tail = None
    if i > 1:
        head = f"<{root_tag}>".encode()
    if i < len(offsets) -1:
        tail = f"</{root_tag}>".encode()

    print(f"Extracting chunk {i}: {prev_offset} -> {offset} ({count:,} bytes) -> {fn_out} (head:{head}, tail:{tail})")

    extract_section( fn_xml, fn_out, prev_offset, count, head, tail )

#print( offsets )
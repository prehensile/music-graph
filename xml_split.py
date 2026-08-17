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

READ_CHUNK = 1 << 20  # 1 MiB


def find_next_tag_end(fp, tag_close, start_pos):
    """
    Find the byte offset just past the next occurrence of tag_close at or after
    start_pos, or None at EOF.

    Reads in 1 MiB blocks rather than a byte at a time. The byte-at-a-time
    version managed ~2.7 MiB/s, which on a ~110 GiB releases dump meant roughly
    12 hours for this step alone; buffered, it runs at disk speed. Blocks
    overlap by len(tag_close)-1 bytes so a match straddling a boundary is not
    missed.
    """
    fp.seek(start_pos)
    lc = len(tag_close)
    overlap = b""
    base = start_pos

    while True:
        block = fp.read(READ_CHUNK)
        if not block:
            return None
        buf = overlap + block
        idx = buf.find(tag_close)
        if idx != -1:
            return base - len(overlap) + idx + lc
        overlap = buf[-(lc - 1):] if lc > 1 else b""
        base += len(block)

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

# Guarantee the final chunk runs to EOF whichever branch above exited the loop.
# The `tag_end is None` break used to leave offsets short of xml_size, which
# dropped the trailing bytes -- including the root's closing tag. Because the
# last chunk is also the one that gets no synthetic tail, it came out
# unterminated and every record in it was silently lost at parse time.
if offsets[-1] != xml_size:
    offsets.append(xml_size)

print( offsets )


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
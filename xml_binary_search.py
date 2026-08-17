#!/usr/bin/env python3

import os
import re
import bisect
import time
from typing import Dict, List, Tuple, Optional
# from xml.etree import ElementTree
# from xml.etree.ElementTree import Element
from io import BytesIO

from lxml.etree import ElementTree, Element, iterparse
import lxml.etree as et



class BinaryTreeNode:
    def __init__( self, id, offset, parent ):
        self.left = None
        self.right = None
        self.id = id
        self.offset = offset
        self.parent = parent


class XMLBinarySearcher:
    """
    A class for performing binary search on large XML files to find specific elements
    by their ID attributes. Maintains a lookup table of element positions for fast
    repeated searches without loading the entire file into memory.
    """


    def __init__(self, xml_file_path: str, tag_name: str):
        """
        Initialize the binary searcher for a specific XML file and tag type.
        
        Args:
            xml_file_path: Path to the XML file
            tag_name: Name of the XML tag to search for (e.g., 'artist', 'release')
        """
        self.xml_file_path = xml_file_path
        self.tag_name = tag_name
        self.file_size = os.path.getsize(xml_file_path)
        self.open_file = open( xml_file_path, "rb" )
        self.iter_parse = iterparse( self.open_file, events=("start",), tag=tag_name )
        
        p = b"<" + tag_name.encode() + r"[\s>]".encode()
        self.tag_matcher_open = re.compile( p )
        self.tag_close = f"</{tag_name}>".encode()

        self.root_node = self.generate_nearest_node( int(self.file_size/2), None )


    def get_nearest_element( self, offset ):
        self.open_file.seek( offset )
        return self.get_next_element()
        
    
    def get_next_element( self ):

        offset =  self.open_file.tell()
        
        buf = b""
        match:re.Match = None
        while not match:
            buf += self.open_file.read(1)
            match = self.tag_matcher_open.search( buf )
            # if buf.endswith( b"<release " ):
            #     pass
            # print(buf)

        element_offset = offset + match.start()
        
        buf = buf[match.start():]
        lc = len( self.tag_close)
        while buf[-lc:] != self.tag_close:
            buf += self.open_file.read(1)

        elem_out = et.fromstring( buf )

        return elem_out, element_offset


    def id_for_element( self, element:Element ) -> int:
        element_id = element.get( "id" )
        if element_id is None:
            element_id = element.find("id").text
        return int( element_id )


    def generate_nearest_node( self, offset, parent ):
        element, element_offset = self.get_nearest_element( offset ) 
        element_id = self.id_for_element( element )
        node_out = BinaryTreeNode( 
            element_id,
            element_offset,
            parent
        )
        element.clear()
        return node_out


    def go_left( self, node, min_offset=0 ):
        if node.left is None:
            left_offset = min_offset + int((node.offset - min_offset) / 2)
            if left_offset >= node.offset:
                return None
            node.left = self.generate_nearest_node( left_offset, parent=node )
        return node.left
    
    def go_right( self, node, max_offset=None ):
        if max_offset is None:
            max_offset = self.file_size
        if node.right is None:
            right_offset = node.offset + int((max_offset - node.offset) / 2)
            if right_offset <= node.offset:
                return None
            node.right = self.generate_nearest_node( right_offset, parent=node )
        return node.right

    def get_sibling( self, node ):
        parent = node.parent
        if node == parent.left:
            return parent.right
        else:
            return parent.left

    def find_closest_smaller_offset(self, node):
        """Walk up the tree to find a node with smaller offset"""
        current = node.parent
        while current is not None:
            if current.offset < node.offset:
                return current
            current = current.parent
        return None

    def fetch_element(self, element_id: int, context_size: int = 200) -> Optional[Element]:
        """
        Fetch an XML element by its ID using binary search or cache lookup.
        
        Args:
            element_id: The ID of the element to find
            context_size: Additional context to read around the element
            
        Returns:
            ElementTree.Element if found, None otherwise
        """
        current_node = self.root_node
        min_offset = 0
        max_offset = self.file_size
        last_node = None
        visited_nodes = set()
        iteration_count = 0
        
        while current_node is not None:
            iteration_count += 1
            # print(f"Iteration {iteration_count}: Checking node ID {current_node.id} at offset {current_node.offset}, target: {element_id}")
            
            diff = element_id - current_node.id
            
            # Better loop detection
            if current_node.id in visited_nodes:
                # print(f"ERROR: Revisited node {current_node.id}, breaking to avoid infinite loop")
                # When we hit a loop, the element likely doesn't exist - try the close search
                if abs(diff) < 100:  # Expand the "close enough" range as fallback
                    # print(f"Attempting fallback search with expanded range")
                    if diff > 0:
                        try:
                            return self.linear_search(current_node.offset, element_id)
                        except:
                            pass
                    else:
                        pred = self.find_closest_smaller_offset(current_node)
                        if pred:
                            try:
                                return self.linear_search(pred.offset, element_id)
                            except:
                                pass
                return None
            visited_nodes.add(current_node.id)
            
            # Safety check for runaway loops
            if iteration_count > 50:
                # print(f"ERROR: Too many iterations ({iteration_count}), breaking")
                return None

            if current_node.id == element_id:
                # Found exact match
                element, _ = self.get_nearest_element(current_node.offset)
                return element
            elif abs(diff) < 10:
                # Close enough - do linear search around this area
                if diff > 0:
                    element = self.linear_search( current_node.offset, element_id )
                else: 
                    # Find a predecessor node and search from there
                    pred = self.find_closest_smaller_offset(current_node)
                    if pred:
                        element = self.linear_search( pred.offset, element_id )
                    else:
                        # No predecessor found, search from beginning
                        element = self.linear_search( 0, element_id )
                return element
            elif element_id > current_node.id:
                min_offset = current_node.offset
                # print(f"Going right from {current_node.id}, min_offset now {min_offset}")
                current_node = self.go_right(current_node, max_offset)
                # if current_node is None:
                    # print("go_right returned None - element likely doesn't exist")
            else:
                max_offset = current_node.offset
                # print(f"Going left from {current_node.id}, max_offset now {max_offset}")
                current_node = self.go_left(current_node, min_offset)
                # if current_node is None:
                #     print("go_left returned None - element likely doesn't exist")
        
        return None
    
    def linear_search(self, offset: int, target_id: int) -> Optional[Element]:
        element_out = None
        e, e_offs = self.get_nearest_element( offset )
        while element_out is None:
            element_id = self.id_for_element( e )
            if element_id == target_id:
                element_out = e
            elif element_id < target_id:
                e, e_offs = self.get_next_element()
            else:
                raise Exception( f"Overshot element with id {target_id}" )
        return element_out
        
if __name__ == "__main__":
    s = XMLBinarySearcher( "data/discogs_20250201_releases.xml", "release" )
    ids = [ 225108, 31871, 39 ]
    for id in ids:
        start = time.time()
        e = s.fetch_element( 225108 )
        print( f"Found element in {time.time()-start} seconds")
        # print( et.tostring(e) )

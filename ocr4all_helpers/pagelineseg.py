# -*- coding: utf-8 -*-
# Line segmentation script for images with PAGE xml.
# Derived from the `import_from_larex.py` script, parts of kraken and ocropy,
# with additional tweaks e.g. pre rotation of text regions
#
# nashi project:
#   https://github.com/andbue/nashi 
# ocropy:
#   https://github.com/tmbdev/ocropy/
# kraken:
#   https://github.com/mittagessen/kraken

import numpy as np
from skimage.measure import find_contours, approximate_polygon
from skimage.draw import line_aa
from scipy.ndimage.filters import gaussian_filter, uniform_filter
import math

from lxml import etree
from PIL import Image
from imagemanipulation import cutout

import lib.morph as morph
import lib.sl as sl
import lib.pseg as pseg
from lib.nlbin import adaptive_binarize, estimate_skew

from multiprocessing.pool import ThreadPool
import json

import argparse

import os

# Add printing for every thread
from threading import Lock
s_print_lock = Lock()
def s_print(*a, **b):
    with s_print_lock:
        print(*a, **b)


class record(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


# Given a line segmentation map, computes a list
# of tuples consisting of 2D slices and masked images.
#
# Implementation derived from ocropy with changes to allow extracting
# the line coords/polygons
def compute_lines(segmentation, smear_strength, scale, growth, max_iterations):
    lobjects = morph.find_objects(segmentation)
    lines = []
    for i, o in enumerate(lobjects):
        if o is None:
            continue
        if sl.dim1(o) < 2*scale or sl.dim0(o) < scale:
            continue
        mask = (segmentation[o] == i+1)
        if np.amax(mask) == 0:
            continue

        result = record()
        result.label = i+1
        result.bounds = o
        polygon = []
        if ((segmentation[o] != 0) == (segmentation[o] != i+1)).any():
            ppoints = approximate_smear_polygon(mask, smear_strength, growth, max_iterations)
            ppoints = ppoints[1:] if ppoints else []
            polygon = [(o[0].start+p[0], o[1].start+p[1]) for p in ppoints]
        if not polygon:
            polygon = [(o[0].start, o[1].start), (o[0].stop,  o[1].start),
                       (o[0].stop,  o[1].stop),  (o[0].start, o[1].stop)]
        result.polygon = polygon
        result.mask = mask
        lines.append(result)
    return lines


def compute_gradmaps(binary, scale, vscale=1.0, hscale=1.0, usegauss=False):
    # use gradient filtering to find baselines
    boxmap = pseg.compute_boxmap(binary, scale)
    cleaned = boxmap*binary
    if usegauss:
        # this uses Gaussians
        grad = gaussian_filter(1.0*cleaned, (vscale*0.3*scale,
                                            hscale*6*scale),order=(1,0))
    else:
        # this uses non-Gaussian oriented filters
        grad = gaussian_filter(1.0*cleaned, (max(4, vscale*0.3*scale),
                                            hscale*scale), order=(1, 0))
        grad = uniform_filter(grad, (vscale,hscale*6*scale))


    def norm_max(a):
        return a/amax(a)

    bottom = norm_max((grad<0)*(-grad))
    top = norm_max((grad>0)*grad)
    return bottom, top, boxmap


def boundary(contour):
    Xmin = np.min(contour[:, 0])
    Xmax = np.max(contour[:, 0])
    Ymin = np.min(contour[:, 1])
    Ymax = np.max(contour[:, 1])

    return [Xmin, Xmax, Ymin, Ymax]


# Approximate a single polygon around high pixels in a mask, via smearing
def approximate_smear_polygon(line_mask, smear_strength=(1, 2), growth=(1.1, 1.1), max_iterations=1000):
    work_image = np.pad(np.copy(line_mask), pad_width=1, mode='constant', constant_values=False)

    contours = find_contours(work_image, 0.5, fully_connected="low")

    if len(contours) > 0:
        iteration = 1
        while len(contours) > 1:
            # Get bounds with dimensions
            bounds = [boundary(contour) for contour in contours]
            widths = [b[1]-b[0] for b in bounds]
            heights = [b[3]-b[2] for b in bounds]

            # Calculate x and y median distances (or at least 1)
            width_median = sorted(widths)[int(len(widths) / 2)]
            height_median = sorted(heights)[int(len(heights) / 2)]

            # Calculate x and y smear distance 
            smear_distance_x = math.ceil(width_median*smear_strength[0] * (iteration*growth[0]))
            smear_distance_y = math.ceil(height_median*smear_strength[1] * (iteration*growth[1]))

            # Smear image in x and y direction
            width, height = work_image.shape
            gaps_current_x = [float('Inf')]*height
            for x in range(width):
                gap_current_y = float('Inf')
                for y in range(height):
                    if work_image[x, y]:
                        # Entered Contour
                        gap_current_x = gaps_current_x[y]

                        if gap_current_y < smear_distance_y and gap_current_y > 0:
                            # Draw over
                            work_image[x, y-gap_current_y:y] = True
                        
                        if gap_current_x < smear_distance_x and gap_current_x > 0:
                            #Draw over
                            work_image[x-gap_current_x:x, y] = True

                        gap_current_y = 0
                        gaps_current_x[y] = 0
                    else:
                        # Entered/Still in Gap
                        gap_current_y += 1
                        gaps_current_x[y] += 1
            # Find contours of current smear
            contours = find_contours(work_image, 0.5, fully_connected="low")

            # Failsave if contours can't be smeared together after x iterations
            # Draw lines between the extreme points of each contour in order
            if iteration >= max_iterations and len(contours) > 1:
                s_print("Start fail save, since precise line generation took too many iterations ({}).".format(iteration))
                extreme_points = []
                for contour in contours:
                    sorted_x = sorted(contour, key=lambda c: c[0])
                    sorted_y = sorted(contour, key=lambda c: c[1])
                    extreme_points.append((tuple(sorted_x[0]), tuple(sorted_y[1]), tuple(sorted_x[-1]), tuple(sorted_y[-1])))
                
                sorted_extreme = sorted(extreme_points, key=lambda e: e)
                for c1, c2 in zip(sorted_extreme, sorted_extreme[1:]):
                    for p1 in c1:
                        nearest = None
                        nearest_dist = math.inf
                        for p2 in c2:
                            distance = math.sqrt((p1[0]-p2[0])**2 + (p1[1]-p2[1])**2)
                            if distance < nearest_dist:
                                nearest = p2
                                nearest_dist = distance
                        if nearest:
                            # Draw line between nearest points
                            xx, yy, _ = line_aa(int(p1[0]), int(nearest[0]), int(p2[1]), int(nearest[1]))
                            # Remove border points
                            line_points = [(x,y) for x,y in zip(xx,yy) if 0 < x < width and 0 < y < height]
                            xx_filtered, yy_filtered = zip(*line_points) 
                            # Paint
                            work_image[xx_filtered, yy_filtered] = True
                contours = find_contours(work_image, 0.5, fully_connected="low")

            iteration += 1

        simplified_contours = approximate_polygon(contours[0], 0.1)
        return [(p[0]-1, p[1]-1) for p in simplified_contours]
    return []
    

def segment(im, scale=None, maxcolseps=2, black_colseps=False, smear_strength=(1,2), growth=(1.1, 1.1), orientation=0, fail_save_iterations=1000):
    """
    Segments a page into text lines.
    Segments a page into text lines and returns the absolute coordinates of
    each line in reading order.
    Args:
        im (PIL.Image): A bi-level page of mode '1' or 'L'
        scale (float): Scale of the image
        maxcolseps (int): Maximum number of whitespace column separators
        black_colseps (bool): Whether column separators are assumed to be
                              vertical black lines or not
        growth (float): Tolerance for the polygons wrapping textlines
    Returns:
        {'boxes': [(x1, y1, x2, y2),...]}: A
        dictionary containing the text direction and a list of reading order
        sorted bounding boxes under the key 'boxes'.
    Raises:
        ValueError if the input image is not binarized or the text
        direction is invalid.
    """

    colors = im.getcolors(2)
    if im.mode != '1' and not (colors is not None and len(colors) == 2):
        raise ValueError('Image is not bi-level')

    # rotate input image for vertical lines
    im_rotated = im.rotate(orientation, expand=True, center=(im.width/2,im.height/2))

    a = np.array(im_rotated.convert('L')) if im_rotated.mode == '1' else np.array(im_rotated)
    binary = np.array(a > 0.5*(np.amin(a) + np.amax(a)), 'i')
    binary = 1 - binary

    if not scale:
        scale = pseg.estimate_scale(binary)

    binary = pseg.remove_hlines(binary, scale)
    # emptyish images will cause exceptions here.
    try:
        colseps, binary = pseg.compute_colseps(binary, scale, maxcolseps, black_colseps)
    except ValueError:
        return []

    bottom, top, boxmap = compute_gradmaps(binary, scale)
    seeds = pseg.compute_line_seeds(binary, bottom, top, colseps, scale)
    llabels1 = morph.propagate_labels(boxmap, seeds, conflict=0)
    spread = morph.spread_labels(seeds, maxdist=scale)
    llabels = np.where(llabels1 > 0, llabels1, spread*binary)
    segmentation = llabels*binary

    lines_and_polygons = compute_lines(segmentation, smear_strength, scale, growth, fail_save_iterations)

    # Translate each point back to original
    deltaX = im_rotated.width - im.width
    deltaY = im_rotated.height - im.height
    centerX = im_rotated.width / 2
    centerY = im_rotated.height / 2

    def translate_back(point):
        transX = point[0] - centerX
        transY = point[1] - centerY
        rotatedX = transX * math.cos(-orientation) - transY * math.sin(-orientation)
        rotatedY = transX * math.sin(-orientation) + transY * math.cos(-orientation)
        return (int(rotatedX-deltaX/2), int(rotatedY-deltaY/2))

    lines_and_polygons = [[translate_back(p) for p in poly] for poly in lines_and_polygons]

    # Sort lines for reading order
    order = pseg.reading_order([l.bounds for l in lines_and_polygons])
    lsort = pseg.topsort(order)
    lines = [lines_and_polygons[i].bounds for i in lsort]
    lines = [(s2.start, s1.start, s2.stop, s1.stop) for s1, s2 in lines]
    return lines


def pagexmllineseg(xmlfile, imgpath, scale=None, maxcolseps=-1, smear_strength=(1, 2), growth=(1.1,1.1), fail_save_iterations=100):
    name = os.path.splitext(os.path.split(imgpath)[-1])[0]
    s_print("""Start process for '{}'
        |- Image: '{}'
        |- Annotations: '{}' """.format(name, imgpath, xmlfile))

    root = etree.parse(xmlfile).getroot()
    ns = {"ns": root.nsmap[None]}

    s_print("[{}] Retrieve TextRegions".format(name))

    # convert point notation from older pagexml versions
    for c in root.xpath("//ns:Coords[not(@points)]", namespaces=ns):
        cc = []
        for point in c.xpath("./ns:Point", namespaces=ns):
            # coordstrings = [x.split(",") for x in c.attrib["points"].split()]
            cx = point.attrib["x"]
            cy = point.attrib["y"]
            c.remove(point)
            cc.append(cx+","+cy)
        c.attrib["points"] = " ".join(cc)

    coordmap = {}
    for r in root.xpath('//ns:TextRegion', namespaces=ns):
        rid = r.attrib["id"]
        coordmap[rid] = {"type": r.attrib["type"]}
        coordmap[rid]["coords"] = []
        for c in r.xpath("./ns:Coords", namespaces=ns) + r.xpath("./Coords"):
            coordmap[rid]["coordstring"] = c.attrib["points"]
            coordstrings = [x.split(",") for x in c.attrib["points"].split()]
            coordmap[rid]["coords"] += [[int(x[0]), int(x[1])]
                                        for x in coordstrings]
        if 'orientation' in r.attrib:
            coordmap[rid]["orientation"] = float(r.attrib["orientation"])

    s_print("[{}] Extract Textlines from TextRegions".format(name))
    im = Image.open(imgpath)

    for n, c in enumerate(sorted(coordmap)):
        if type(scale) == dict:
            if coordmap[c]['type'] in scale:
                rscale = scale[coordmap[c]['type']]
            elif "other" in scale:
                rscale = scale["other"]
            else:
                rscale = None
        else:
            rscale = scale
        coords = coordmap[c]['coords']
        
        if len(coords) < 3:
            continue
        cropped = cutout(im, coords)

        if 'orientation' in coordmap[c]:
            orientation = coordmap[c]['orientation']
        else:
            orientation = estimate_skew(cropped)

        offset = (min([x[0] for x in coords]), min([x[1] for x in coords]))
        if cropped is not None:
            colors = cropped.getcolors(2)
            if not (colors is not None and len(colors) == 2):
                try:
                    cropped = adaptive_binarize(cropped)
                except SystemError:
                    continue
            if coordmap[c]["type"] == "drop-capital":
                lines = [1]
            else:
                # if line in
                lines = segment(cropped, scale=rscale, maxcolseps=maxcolseps,
                                smear_strength=smear_strength, growth=growth,
                                orientation=orientation,
                                fail_save_iterations=fail_save_iterations)

        else:
            lines = []

        # Iterpret whole region as textline if no textline are found
        if not(lines) or len(lines) == 0:
            coordstrg = " ".join([str(x[0])+","+str(x[1]) for x in coords])
            textregion = root.xpath('//ns:TextRegion[@id="'+c+'"]', namespaces=ns)[0]
            if orientation:
                textregion.attrib['orientation'] = orientation
            linexml = etree.SubElement(textregion, "TextLine",
                                       attrib={"id": "{}_l{:03d}".format(c, n+1)})
            etree.SubElement(linexml, "Coords", attrib={"points": coordstrg})

        else:
            for n, poly in enumerate(lines):
                if coordmap[c]["type"] == "drop-capital":
                    coordstrg = coordmap[c]["coordstring"]
                else:
                    coords = ((x[1]+offset[0], x[0]+offset[1]) for x in poly)
                    coordstrg = " ".join([str(int(x[0]))+","+str(int(x[1])) for x in coords])
                textregion = root.xpath('//ns:TextRegion[@id="'+c+'"]', namespaces=ns)[0]
                if orientation:
                    textregion.attrib['orientation'] = orientation
                linexml = etree.SubElement(textregion, "TextLine",
                                           attrib={"id": "{}_l{:03d}".format(c, n+1)})
                etree.SubElement(linexml, "Coords", attrib={"points": coordstrg})

    s_print("[{}] Generate new PAGE xml with textlines".format(name))
    xmlstring = etree.tounicode(root.getroottree()).replace(
        "http://schema.primaresearch.org/PAGE/gts/pagecontent/2010-03-19",
        "http://schema.primaresearch.org/PAGE/gts/pagecontent/2017-07-15")
    no_lines_segm = int(root.xpath("count(//TextLine)"))
    return xmlstring, no_lines_segm


def main():
    parser = argparse.ArgumentParser("""
    Line segmentation with regions read from a PAGE xml file
    """)
    parser.add_argument('DATASET',type=str,help='Path to the input dataset in json format with a list of image path, pagexml path and optional output path. (Will overwrite pagexml if no output path is given)') 
    parser.add_argument('-s','--scale', type=float, default=None, help='Scale of the input image used for the line segmentation. Will be estimated if not defined.')
    parser.add_argument('-p','--parallel', type=int, default=1, help='Number of threads parallely working on images. (default:%(default)s)')
    parser.add_argument('-x','--smearX', type=float, default=2, help='Smearing strength in X direction for the algorithm calculating the textline polygon wrapping all contents. (default:%(default)s)')
    parser.add_argument('-y','--smearY', type=float, default=1, help='Smearing strength in Y direction for the algorithm calculating the textline polygon wrapping all contents. (default:%(default)s)')
    parser.add_argument('--growthX', type=float, default=1.1, help='Growth in X direction for every iteration of the Textline polygon finding. Will speed up the algorithm at the cost of precision. (default: %(default)s)')
    parser.add_argument('--growthY', type=float, default=1.1, help='Growth in Y direction for every iteration of the Textline polygon finding. Will speed up the algorithm at the cost of precision. (default: %(default)s)')
    parser.add_argument('--maxcolseps', type=int, default=-1, help='Maximum # whitespace column separators, (default: %(default)s)')
    parser.add_argument('--fail_save', type=int, default=1000, help='Fail save to counter infinite loops when combining contours to a precise textlines. Will connect remaining contours with lines. (default: %(default)s)')
                    
    args = parser.parse_args()

    with open(args.DATASET, 'r') as data_file:
        dataset = json.load(data_file)

    # Parallel processes for the pagexmllineseg
    def parallel(data):
        image,pagexml = data[:2]
        pagexml_out = data[2] if (len(data) > 2 and data[2] is not None) else pagexml

        xml_output, number_lines = pagexmllineseg(pagexml, image, 
                                                    scale=args.scale,
                                                    maxcolseps=args.maxcolseps, 
                                                    smear_strength=(args.smearX, args.smearY), 
                                                    growth=(args.growthX,args.growthY),
                                                    fail_save_iterations=args.fail_save)
        with open(pagexml_out, 'w+') as output_file:
            s_print("Save annotations into '{}'".format(pagexml_out))
            output_file.write(xml_output)
    
    s_print("Process {} images, with {} in parallel".format(len(dataset), args.parallel))

    # Pool of all parallel processed pagexmllineseg
    with ThreadPool(processes=min(args.parallel, len(dataset))) as pool:
        output = pool.map(parallel, dataset)
    

if __name__ == "__main__":
    main()

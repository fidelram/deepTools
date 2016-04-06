#!/usr/bin/env python
# -*- coding: utf-8 -*-

# TODO: the genome chunk length is terrible for this sort of operation
#       Is it faster to query all intervals in a chunk and then fetch the reads (probably not)?
#       Should the percents or the counts be output? The former are more generally useful
#       Test all of the parameters and make some doc/nose tests
#       Galaxy wrapper
#       What should the default dimensions be? I get the feeling that the font isn't scaling nicely

import sys
import argparse
import numpy as np
from matplotlib import use as mplt_use
mplt_use('Agg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.font_manager import FontProperties

from deeptools.mapReduce import mapReduce
from deeptools import parserCommon
from deeptools.getScaleFactor import fraction_kept
from deeptools.getFragmentAndReadSize import get_read_and_fragment_length
from deeptools.utilities import getCommonChrNames, mungeChromosome
from deeptools.bamHandler import openBam
from deeptoolsintervals import Enrichment


old_settings = np.seterr(all='ignore')


def parse_arguments(args=None):
    basic_args = plot_enrichment_args()

    # --region, --blackListFileName, -p and -v
    parent_parser = parserCommon.getParentArgParse(binSize=False)

    # --extend reads and such
    read_options = parserCommon.read_options()

    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""
Tool for calculating and plotting the signal enrichment in either regions in BED format or feature types (column 3) in GTF format. The underlying datapoints can also be output. Metrics are plotted as a fraction of total reads. Regions in a BED file are assigned to the 'peak' feature.

detailed help:

  plotEnrichment -h

""",
        epilog='example usages:\n'
               'plotEnrichment -b file1.bam file2.bam --BED peaks.bed -o enrichment.png\n\n'
               ' \n\n',
        parents=[basic_args, parent_parser, read_options])

    return parser


def plot_enrichment_args():
    parser = argparse.ArgumentParser(add_help=False)
    required = parser.add_argument_group('Required arguments')

    # define the arguments
    required.add_argument('--bamfiles', '-b',
                          metavar='file1.bam file2.bam',
                          help='List of indexed bam files separated by spaces.',
                          nargs='+',
                          required=True)

    required.add_argument('--BED',
                          help='Limits the enrichment analysis to '
                          'the regions specified in these BED/GTF files. Enrichment '
                          'is calculated as the number of reads overlapping each '
                          'feature type. The feature type is column 3 in a GTF file '
                          'and "peak" for BED files.',
                          metavar='FILE1.bed FILE2.bed',
                          nargs='+',
                          required=True)

    required.add_argument('--plotFile', '-o',
                          help='File to save the plot to. The file extension determines the format, '
                          'so heatmap.pdf will save the heatmap in PDF format. '
                          'The available formats are: .png, '
                          '.eps, .pdf and .svg.',
                          type=argparse.FileType('w'),
                          metavar='FILE',
                          required=True)

    optional = parser.add_argument_group('Optional arguments')

    optional.add_argument('--labels', '-l',
                          metavar='sample1 sample2',
                          help='User defined labels instead of default labels from '
                          'file names. '
                          'Multiple labels have to be separated by spaces, e.g. '
                          '--labels sample1 sample2 sample3',
                          nargs='+')

    optional.add_argument('--plotTitle', '-T',
                          help='Title of the plot, to be printed on top of '
                          'the generated image. Leave blank for no title.',
                          default='')

    optional.add_argument('--plotFileFormat',
                          metavar='FILETYPE',
                          help='Image format type. If given, this option '
                          'overrides the image format based on the plotFile '
                          'ending. The available options are: png, '
                          'eps, pdf and svg.',
                          choices=['png', 'pdf', 'svg', 'eps'])

    optional.add_argument('--outRawCounts',
                          help='Save the counts per region to a tab-delimited file.',
                          metavar='FILE',
                          type=argparse.FileType('w'))

    optional.add_argument('--perSample',
                          help='Group the plots by sample, rather than by feature type (the default).',
                          action='store_true')

    optional.add_argument('--plotHeight',
                          help='Plot height in cm.',
                          type=float,
                          default=20)

    optional.add_argument('--plotWidth',
                          help='Plot width in cm. The minimum value is 1 cm.',
                          type=float,
                          default=20)

    optional.add_argument('--colors',
                          help='List of colors to use '
                          'for the plotted lines. Color names '
                          'and html hex strings (e.g., #eeff22) '
                          'are accepted. The color names should '
                          'be space separated. For example, '
                          '--colors red blue green ',
                          nargs='+')

    optional.add_argument('--numPlotsPerRow',
                          help='Number of plots per row',
                          type=int,
                          default=4)

    optional.add_argument('--alpha',
                          default=0.9,
                          type=parserCommon.check_float_0_1,
                          help='The alpha channel (transparency) to use for the bars. '
                          'The default is 0.9 and values must be between 0 and 1.')

    bed12 = parser.add_argument_group('BED12 arguments')

    bed12.add_argument('--keepExons',
                       help="For BED12 files, use each exon as a region, rather than columns 2/3",
                       action="store_true")

    return parser


def getBAMBlocks(read, defaultFragmentLength, centerRead):
    """
    This is basically get_fragment_from_read from countReadsPerBin
    """
    def is_proper_pair():
        """
        Checks if a read is proper pair meaning that both mates are facing each other and are in
        the same chromosome and are not to far away. The sam flag for proper pair can not
        always be trusted.
        :return: bool
        """
        if not read.is_proper_pair:
            return False
        if read.reference_id != read.next_reference_id:
            return False
        if maxPairedFragmentLength > abs(read.template_length) > 0:
            return False
        # check that the mates face each other (inward)
        if read.reference_start < read.next_reference_start and not read.is_reverse and read.mate_is_reverse:
            return True
        if read.reference_start >= read.next_reference_start and read.is_reverse and not read.mate_is_reverse:
            return True
        return False

    maxPairedFragmentLength = 0
    if defaultFragmentLength != "read length":
        maxPairedFragmentLength = 4 * defaultFragmentLength

    if defaultFragmentLength == 'read length':
        return read.get_blocks()
    else:
        if is_proper_pair():
            if read.is_reverse:
                fragmentStart = read.next_reference_start
                fragmentEnd = read.reference_end
            else:
                fragmentStart = read.reference_start
                # the end of the fragment is defined as
                # the start of the forward read plus the insert length
                fragmentEnd = read.reference_start + abs(read.template_length)
        # Extend using the default fragment length
        else:
            if read.is_reverse:
                fragmentStart = read.reference_end - defaultFragmentLength
                fragmentEnd = read.reference_end
            else:
                fragmentStart = read.reference_start
                fragmentEnd = read.reference_start + defaultFragmentLength
        if centerRead:
            fragmentCenter = fragmentEnd - (fragmentEnd - fragmentStart) / 2
            fragmentStart = fragmentCenter - read.query_length / 2
            fragmentEnd = fragmentStart + read.query_length

        assert fragmentStart < fragmentEnd, "fragment start greater than fragment" \
                                            "end for read {}".format(read.query_name)
        return [(fragmentStart, fragmentEnd)]


def getEnrichment_worker(arglist):
    """
    This is the worker function of plotEnrichment.

    In short, given a region, iterate over all reads **starting** in it.
    Filter/extend them as requested and check each for an overlap with
    findOverlaps. For each overlap, increment the counter for that feature.
    """
    chrom, start, end, args, defaultFragmentLength = arglist

    gtf = Enrichment(args.BED, keepExons=args.keepExons)
    olist = []
    for f in args.bamfiles:
        odict = dict()
        for x in gtf.features:
            odict[x] = 0
        fh = openBam(f)

        chrom = mungeChromosome(chrom, fh.references)

        prev_start_pos = None  # to store the start positions
        for read in fh.fetch(chrom, start, end):
            # Filter
            if read.pos < start:
                # Ensure that a given alignment is processed only once
                continue
            if read.flag & 4:
                continue
            if args.minMappingQuality and read.mapq < args.minMappingQuality:
                continue
            if args.samFlagInclude and read.flag & args.samFlagInclude == 0:
                continue
            if args.samFlagExclude and read.flag & args.samFlagExclude != 0:
                continue
            if args.ignoreDuplicates and prev_start_pos \
                    and prev_start_pos == (read.reference_start, read.pnext, read.is_reverse):
                continue
            prev_start_pos = (read.reference_start, read.pnext, read.is_reverse)

            # Get blocks, possibly extending
            features = gtf.findOverlaps(chrom, getBAMBlocks(read, defaultFragmentLength, args.centerReads))

            if features is not None and len(features) > 0:
                for x in features:
                    odict[x] += 1
        olist.append(odict)
    return olist, gtf.features


def plotEnrichment(args, featureCounts, totalCounts, features):
    # get the number of rows and columns
    if args.perSample:
        totalPlots = len(args.bamfiles)
        barsPerPlot = len(features)
    else:
        totalPlots = len(features)
        barsPerPlot = len(args.bamfiles)
    cols = min(args.numPlotsPerRow, totalPlots)
    rows = np.ceil(totalPlots / float(args.numPlotsPerRow)).astype(int)

    # Handle the colors
    if not args.colors:
        cmap_plot = plt.get_cmap('jet')
        args.colors = cmap_plot(np.arange(barsPerPlot, dtype=float) / float(barsPerPlot))
    if len(args.colors) < barsPerPlot:
        sys.exit("Error: {0} colors were requested, but {1} were needed!".format(len(args.colors), barsPerPlot))

    grids = gridspec.GridSpec(rows, cols)
    plt.rcParams['font.size'] = 10.0
    font_p = FontProperties()
    font_p.set_size('small')

    # convert cm values to inches
    fig = plt.figure(figsize=(args.plotWidth / 2.54, args.plotHeight / 2.54))
    fig.suptitle(args.plotTitle, y=(1 - (0.06 / args.plotHeight)))

    for i in range(totalPlots):
        col = i % cols
        row = np.floor(i / float(args.numPlotsPerRow)).astype(int)

        if args.perSample:
            xlabels = features
            ylabel = "% alignments in {0}".format(args.labels[i])
            vals = [featureCounts[i][foo] for foo in features]
            vals = 100 * np.array(vals, dtype='float64') / totalCounts[i]
        else:
            xlabels = args.labels
            ylabel = "% {0}".format(features[i])
            vals = [foo[features[i]] for foo in featureCounts]
            vals = 100 * np.array(vals, dtype='float64') / np.array(totalCounts, dtype='float64')
        ax = plt.subplot(grids[row, col])
        ax.bar(np.arange(vals.shape[0]), vals, width=1.0, bottom=0.0, align='center', color=args.colors, edgecolor=args.colors, alpha=args.alpha)
        ax.set_ylabel(ylabel)
        ax.set_xticks(np.arange(vals.shape[0]))
        ax.set_xticklabels(xlabels, rotation='vertical')
        ax.set_ylim(0.0, 100.0)

    plt.subplots_adjust(wspace=0.05, hspace=0.3, bottom=0.15, top=0.80)
    plt.tight_layout()
    plt.savefig(args.plotFile, dpi=200, format=args.plotFileFormat)
    plt.close()


def main(args=None):

    args = parse_arguments().parse_args(args)

    if args.labels is None:
        args.labels = args.bamfiles
    if len(args.labels) != len(args.bamfiles):
        sys.exit("Error: The number of labels ({0}) does not match the number of BAM files ({1})!".format(len(args.labels), len(args.bamfiles)))

    # Get the total counts, excluding blacklisted regions and filtered reads
    totalCounts = []
    fhs = [openBam(x) for x in args.bamfiles]
    for bam_handle in fhs:
        bam_mapped = parserCommon.bam_total_reads(bam_handle, None)
        blacklisted = parserCommon.bam_blacklisted_reads(bam_handle, None, args.blackListFileName)
        if args.verbose:
            print(("There are {0} alignments in {1}, of which {2} are completely within a blacklist region.".format(bam_mapped, bam_handle.name, blacklisted)))
        bam_mapped -= blacklisted
        args.bam = bam_handle.filename
        args.ignoreForNormalization = None
        ftk = fraction_kept(args)
        bam_mapped *= ftk
        totalCounts.append(bam_mapped)

    # Get fragment size and chromosome dict
    chromSize, non_common_chr = getCommonChrNames(fhs, verbose=args.verbose)
    for fh in fhs:
        fh.close()

    frag_len_dict, read_len_dict = get_read_and_fragment_length(args.bamfiles[0],
                                                                return_lengths=False,
                                                                blackListFileName=args.blackListFileName,
                                                                numberOfProcessors=args.numberOfProcessors,
                                                                verbose=args.verbose)
    if args.extendReads:
        if args.extendReads is True:
            # try to guess fragment length if the bam file contains paired end reads
            if frag_len_dict:
                defaultFragmentLength = frag_len_dict['median']
            else:
                sys.exit("*ERROR*: library is not paired-end. Please provide an extension length.")
            if args.verbose:
                print("Fragment length based on paired en data "
                      "estimated to be {0}".format(frag_len_dict['median']))
        elif args.extendReads < read_len_dict['median']:
            sys.stderr.write("*WARNING*: read extension is smaller than read length (read length = {}). "
                             "Reads will not be extended.\n".format(int(read_len_dict['median'])))
            defaultFragmentLength = 'read length'
        elif args.extendReads > 2000:
            sys.exit("*ERROR*: read extension must be smaller that 2000. Value give: {} ".format(args.extendReads))
        else:
            defaultFragmentLength = args.extendReads
    else:
        defaultFragmentLength = 'read length'

    # Map reduce to get the counts/file/feature
    res = mapReduce([args, defaultFragmentLength],
                    getEnrichment_worker,
                    chromSize,
                    region=args.region,
                    blackListFileName=args.blackListFileName,
                    numberOfProcessors=args.numberOfProcessors,
                    verbose=args.verbose)

    features = res[0][1]
    featureCounts = []
    for i in list(range(len(args.bamfiles))):
        d = dict()
        for x in features:
            d[x] = 0
        featureCounts.append(d)

    # res is a list, with each element a list (length len(args.bamfiles)) of dicts
    for x in res:
        for i, y in enumerate(x[0]):
            for k, v in y.items():
                featureCounts[i][k] += v

    # Make a plot
    plotEnrichment(args, featureCounts, totalCounts, features)

    # Raw counts
    if args.outRawCounts:
        args.outRawCounts.write("file\tfeatureType\tnumberInFeature\ttotalAlignments\n")
        for i, x in enumerate(args.labels):
            for k, v in featureCounts[i].items():
                args.outRawCounts.write("{0}\t{1}\t{2}\t{3}\n".format(x, k, v, int(totalCounts[i])))
        args.outRawCounts.close()

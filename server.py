import json
import markdown2
import os
import pysam
import re
import socket
import subprocess
import sys
import tempfile
from datetime import datetime
from flask import Flask, request, Response
from flask_cors import CORS
from flask_talisman import Talisman
from spliceai.utils import Annotator, get_delta_scores

app = Flask(__name__)
Talisman(app)
CORS(app)

HG19_FASTA_PATH = os.path.expanduser("~/hg19.fa")
HG38_FASTA_PATH = os.path.expanduser("~/hg38.fa")


SPLICEAI_CACHE_FILES = {}
if socket.gethostname() == "spliceai-lookup":
    for filename in [
        "spliceai_scores.masked.indel.hg19.vcf.gz",
        "spliceai_scores.masked.indel.hg38.vcf.gz",
        "spliceai_scores.masked.snv.hg19.vcf.gz",
        "spliceai_scores.masked.snv.hg38.vcf.gz",
        "spliceai_scores.raw.indel.hg19.vcf.gz",
        "spliceai_scores.raw.indel.hg38.vcf.gz",
        "spliceai_scores.raw.snv.hg19.vcf.gz",
        "spliceai_scores.raw.snv.hg38.vcf.gz",
    ]:
        key = tuple(filename.replace("spliceai_scores.", "").replace(".vcf.gz", "").split("."))
        full_path = os.path.join("/mnt/disks/cache", filename)
        if os.path.isfile(full_path):
            SPLICEAI_CACHE_FILES[key] = pysam.TabixFile(full_path)
else:
    SPLICEAI_CACHE_FILES = {
        ("raw", "indel", "hg38"): pysam.TabixFile("./test_data/spliceai_scores.raw.indel.hg38_subset.vcf.gz"),
        ("raw", "snv", "hg38"): pysam.TabixFile("./test_data/spliceai_scores.raw.snv.hg38_subset.vcf.gz"),
        ("masked", "snv", "hg38"): pysam.TabixFile("./test_data/spliceai_scores.masked.snv.hg38_subset.vcf.gz"),
    }

SPLICEAI_ANNOTATOR = {
    "37": Annotator(HG19_FASTA_PATH, "grch37"),
    "38": Annotator(HG38_FASTA_PATH, "grch38"),
}

SPLICEAI_MAX_DISTANCE_LIMIT = 10000
SPLICEAI_DEFAULT_DISTANCE = 50  # maximum distance between the variant and gained/lost splice site, defaults to 50
SPLICEAI_DEFAULT_MASK = 0  # mask scores representing annotated acceptor/donor gain and unannotated acceptor/donor loss, defaults to 0

SPLICEAI_SCORE_FIELDS = "ALLELE|SYMBOL|DS_AG|DS_AL|DS_DG|DS_DL|DP_AG|DP_AL|DP_DG|DP_DL".split("|")

SPLICEAI_EXAMPLE = f"/spliceai/?hg=38&distance=50&mask=0&variant=chr8-140300615-C-G"

VARIANT_RE = re.compile(
    "(chr)?(?P<chrom>[0-9XYMTt]{1,2})"
    "[-\s:]+"
    "(?P<pos>[0-9]{1,9})"
    "[-\s:]+"
    "(?P<ref>[ACGT]+)"
    "[-\s:>]+"
    "(?P<alt>[ACGT]+)"
)


def error_response(error_message):
    return Response(json.dumps({"error": str(error_message)}), status=400, mimetype='application/json')


REVERSE_COMPLEMENT_MAP = dict(zip("ACGTN", "TGCAN"))


def reverse_complement(seq):
    return "".join([REVERSE_COMPLEMENT_MAP[n] for n in seq[::-1]])


def parse_variant(variant_str):
    match = VARIANT_RE.match(variant_str)
    if not match:
        raise ValueError(f"Unable to parse variant: {variant_str}")

    return match['chrom'], int(match['pos']), match['ref'], match['alt']


class VariantRecord:
    def __init__(self, chrom, pos, ref, alt):
        self.chrom = chrom
        self.pos = pos
        self.ref = ref
        self.alts = [alt]

    def __repr__(self):
        return f"{self.chrom}-{self.pos}-{self.ref}-{self.alts[0]}"


def process_variant(variant, genome_version, spliceai_distance, spliceai_mask):
    try:
        chrom, pos, ref, alt = parse_variant(variant)
    except ValueError as e:
        return {
            "variant": variant,
            "error": f"ERROR: {e}",
        }

    if len(ref) > 1 and len(alt) > 1:
        return {
            "variant": variant,
            "error": f"ERROR: SpliceAI does not currently support complex InDels like {chrom}-{pos}-{ref}-{alt}",
        }

    source = None
    scores = []
    if (len(ref) <= 5 or len(alt) <= 2) and spliceai_distance == SPLICEAI_DEFAULT_DISTANCE:
        # examples: ("masked", "snv", "hg19")  ("raw", "indel", "hg38")
        key = (
            "masked" if spliceai_mask == 1 else ("raw" if spliceai_mask == 0 else None),
            "snv" if len(ref) == 1 and len(alt) == 1 else "indel",
            "hg19" if genome_version == "37" else ("hg38" if genome_version == "38" else None),
        )
        try:
            results = SPLICEAI_CACHE_FILES[key].fetch(chrom, pos-1, pos+1)
            for line in results:
                # ['1', '739023', '.', 'C', 'CT', '.', '.', 'SpliceAI=CT|AL669831.1|0.00|0.00|0.00|0.00|-1|-37|-48|-37']
                fields = line.split("\t")
                if fields[0] == chrom and int(fields[1]) == pos and fields[3] == ref and fields[4] == alt:
                    scores.append(fields[7])
            if scores:
                source = "lookup"
                #print(f"Fetched: ", scores, flush=True)

        except Exception as e:
            print(f"ERROR: couldn't retrieve scores using tabix: {type(e)}: {e}", flush=True)

    if not scores:
        record = VariantRecord(chrom, pos, ref, alt)
        try:
            scores = get_delta_scores(
                record,
                SPLICEAI_ANNOTATOR[genome_version],
                spliceai_distance,
                spliceai_mask)
            source = "computed"
            #print(f"Computed: ", scores, flush=True)
        except Exception as e:
            return {
                "variant": variant,
                "error": f"ERROR: {type(e)}: {e}",
            }

    if not scores:
        return {
            "variant": variant,
            "error": f"ERROR: Unable to compute scores for {variant}. Please check that the genome version and reference allele are correct, and the variant is either exonic or intronic in Gencode v24.",
        }

    scores = [s[s.index("|")+1:] for s in scores]  # drop allele field

    return {
        "variant": variant,
        "genome_version": genome_version,
        "chrom": chrom,
        "pos": pos,
        "ref": ref,
        "alt": alt,
        "scores": scores,
        "source": source,
    }


@app.route("/spliceai/", methods=['POST', 'GET'])
def run_spliceai():

    # check params
    params = {}
    if request.values:
        params.update(request.values)

    if 'variant' not in params:
        params.update(request.get_json(force=True, silent=True) or {})

    variant = params.get('variant', '')
    variant = variant.strip().strip("'").strip('"').strip(",")
    if not variant:
        return error_response(f'"variant" not specified. For example: {SPLICEAI_EXAMPLE}\n')

    if not isinstance(variant, str):
        return error_response(f'"variant" value must be a string rather than a {type(variant)}.\n')

    genome_version = params.get("hg")
    if not genome_version:
        return error_response(f'"hg" not specified. The URL must include an "hg" arg: hg=37 or hg=38. For example: {SPLICEAI_EXAMPLE}\n')

    if genome_version not in ("37", "38"):
        return error_response(f'Invalid "hg" value: "{genome_version}". The value must be either "37" or "38". For example: {SPLICEAI_EXAMPLE}\n')

    spliceai_distance = params.get("distance", SPLICEAI_DEFAULT_DISTANCE)
    try:
        spliceai_distance = int(spliceai_distance)
    except Exception as e:
        return error_response(f'Invalid "distance": "{spliceai_distance}". The value must be an integer.\n')

    if spliceai_distance > SPLICEAI_MAX_DISTANCE_LIMIT:
        return error_response(f'Invalid "distance": "{spliceai_distance}". The value must be < {SPLICEAI_MAX_DISTANCE_LIMIT}.\n')

    spliceai_mask = params.get("mask", str(SPLICEAI_DEFAULT_MASK))
    if spliceai_mask not in ("0", "1"):
        return error_response(f'Invalid "mask" value: "{spliceai_mask}". The value must be either "0" or "1". For example: {SPLICEAI_EXAMPLE}\n')

    spliceai_mask = int(spliceai_mask)

    start_time = datetime.now()
    prefix = start_time.strftime("%m/%d/%Y %H:%M:%S") + f" t{os.getpid()}"
    if request.remote_addr != "63.143.42.242":  # ignore up-time checks
        print(f"{prefix}: {request.remote_addr}: ======================", flush=True)
        print(f"{prefix}: {request.remote_addr}: {variant} processing with hg={genome_version}, distance={spliceai_distance}, mask={spliceai_mask}", flush=True)

    results = process_variant(variant, genome_version, spliceai_distance, spliceai_mask)

    if request.remote_addr != "63.143.42.242":
        print(f"{prefix}: {request.remote_addr}: {variant} results: {results}", flush=True)
        print(f"{prefix}: {request.remote_addr}: {variant} took " + str(datetime.now() - start_time), flush=True)

    status = 400 if results.get("error") else 200
    return Response(json.dumps(results), status=status, mimetype='application/json')


LIFTOVER_EXAMPLE = f"/liftover/?hg=hg19-to-hg38&format=interval&chrom=chr8&start=140300615&end=140300620"

CHAIN_FILE_PATHS = {
    "hg19-to-hg38": "hg19ToHg38.over.chain.gz",
    "hg38-to-hg19": "hg38ToHg19.over.chain.gz",
}


def run_UCSC_liftover_tool(hg, chrom, start, end):
    if hg not in CHAIN_FILE_PATHS:
        raise ValueError(f"Unexpected hg arg value: {hg}")
    chain_file_path = CHAIN_FILE_PATHS[hg]

    reason_liftover_failed = ""
    with tempfile.NamedTemporaryFile(suffix=".bed", mode="wt", encoding="UTF-8") as input_file, \
        tempfile.NamedTemporaryFile(suffix=".bed", mode="rt", encoding="UTF-8") as output_file, \
        tempfile.NamedTemporaryFile(suffix=".bed", mode="rt", encoding="UTF-8") as unmapped_output_file:

        #  command syntax: liftOver oldFile map.chain newFile unMapped
        chrom = "chr" + chrom.replace("chr", "")
        input_file.write("\t".join(map(str, [chrom, start, end, ".", "0", "+"])) + "\n")
        input_file.flush()
        command = f"liftOver {input_file.name} {chain_file_path} {output_file.name} {unmapped_output_file.name}"

        try:
            subprocess.check_output(command, shell=True, encoding="UTF-8")
            results = output_file.read()

            print(f"{hg} liftover on {chrom}:{start}-{end} returned: {results}")

            result_fields = results.strip().split("\t")
            if len(result_fields) > 5:
                result_fields[1] = int(result_fields[1])
                result_fields[2] = int(result_fields[2])

                return {
                    "hg": hg,
                    "chrom": chrom,
                    "start": start,
                    "end": end,
                    "output_chrom": result_fields[0],
                    "output_start": result_fields[1],
                    "output_end": result_fields[2],
                    "output_strand": result_fields[5],
                }
            else:
                reason_liftover_failed = unmapped_output_file.readline().replace("#", "").strip()
        except Exception as e:
            raise ValueError(f"liftOver command failed: {e}")

    if reason_liftover_failed:
        raise ValueError(f"Lift over failed: {reason_liftover_failed}")
    else:
        raise ValueError(f"Lift over failed for unknown reasons")


@app.route("/liftover/", methods=['POST', 'GET'])
def run_liftover():

    # check params
    params = {}
    if request.values:
        params.update(request.values)

    if "format" not in params:
        params.update(request.get_json(force=True, silent=True) or {})

    VALID_HG_VALUES = ("hg19-to-hg38", "hg38-to-hg19")
    hg = params.get("hg")  # "hg19-to-hg38"
    if not hg or hg not in VALID_HG_VALUES:
        return error_response(f'"hg" param error. It should be set to {" or ".join(VALID_HG_VALUES)}. For example: {LIFTOVER_EXAMPLE}\n')

    VALID_FORMAT_VALUES = ("interval", "variant", "position")
    format = params.get("format", "")
    if not format or format not in VALID_FORMAT_VALUES:
        return error_response(f'"format" param error. It should be set to {" or ".join(VALID_FORMAT_VALUES)}. For example: {LIFTOVER_EXAMPLE}\n')

    chrom = params.get("chrom")
    if not chrom:
        return error_response(f'"chrom" param not specified')
    if format == "interval":
        start = params.get("start")
        end = params.get("end")
        if not start:
            return error_response(f'"start" param not specified')
        if not end:
            return error_response(f'"end" param not specified')
    elif format == "position" or format == "variant":
        pos = params.get("pos")
        if not pos:
            return error_response(f'"pos" param not specified')

        pos = int(pos)
        start = pos - 1
        end = pos

    try:
        result = run_UCSC_liftover_tool(hg, chrom, start, end)
    except Exception as e:
        return error_response(str(e))

    result["format"] = format
    if format == "position" or format == "variant":
        result["pos"] = pos
        result["output_pos"] = result["output_end"]

    if format == "variant":
        result["ref"] = params.get("ref")
        result["alt"] = params.get("alt")
        result["output_ref"] = result["ref"]
        result["output_alt"] = result["alt"]
        if result["output_strand"] == "-":
            result["output_ref"] = reverse_complement(result["output_ref"])
            result["output_alt"] = reverse_complement(result["output_alt"])

    return Response(json.dumps(result), mimetype='application/json')


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>/')
def catch_all(path):
    with open("README.md") as f:
        return markdown2.markdown(f.read())


print("Initialization completed.", flush=True)

if __name__ == "__main__":
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

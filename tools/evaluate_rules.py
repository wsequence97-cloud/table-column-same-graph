"""Reproduce labelled evaluation and the fixed 600-table benchmark without services."""
import argparse
from collections import Counter
import csv
import hashlib
from itertools import combinations
import json
from pathlib import Path
import platform
import random
import subprocess
import sys
import time
import tracemalloc
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
BASELINE = 'bebe863'


def baseline_module():
    package = types.ModuleType('baseline_v1')
    package.__path__ = []
    sys.modules[package.__name__] = package
    for name in ('config', 'models', 'text_utils', 'similarity'):
        source = subprocess.check_output(
            ['git', 'show', f'{BASELINE}:src/table_column_same_graph/{name}.py'], cwd=ROOT
        ).decode('utf-8').replace('table_column_same_graph', 'baseline_v1')
        module = types.ModuleType('baseline_v1.' + name)
        module.__package__ = 'baseline_v1'
        module.__file__ = str(ROOT / 'src' / 'table_column_same_graph' / (name + '.py'))
        sys.modules[module.__name__] = module
        setattr(package, name, module)
        exec(compile(source, module.__file__, 'exec'), module.__dict__)
    return package.similarity


def metrics(cases):
    counts = Counter(('TP' if predicted else 'FN') if label else ('FP' if predicted else 'TN')
                     for label, predicted in cases)
    tp, fp, fn = (counts[k] for k in ('TP', 'FP', 'FN'))
    p = tp / (tp + fp) if tp + fp else 0
    r = tp / (tp + fn) if tp + fn else 0
    return {**{k: counts[k] for k in ('TP', 'FP', 'FN', 'TN')},
            'precision': p, 'recall': r, 'f1': 2*p*r/(p+r) if p+r else 0}


def evaluate(args):
    import openpyxl
    from table_column_same_graph import similarity as current
    old = baseline_module()
    cases = list(csv.DictReader(args.csv.open(encoding='utf-8-sig')))
    assert len(cases) == 52 and len({r['case_id'] for r in cases}) == 52
    workbook = openpyxl.load_workbook(args.workbook, data_only=False)
    field_rows = list(workbook.worksheets[1].values)[1:]
    table_rows = list(workbook.worksheets[2].values)[1:]
    by_id = {r['case_id']: r for r in cases}
    # Check inputs and labels, ignoring the workbook's previously computed scores.
    for row in field_rows + table_rows:
        case = by_id[row[0]]
        assert [str(v or '') for v in row[2:6]] == [case[k] for k in (
            'source_name', 'source_comment', 'target_name', 'target_comment')]
        assert int(row[6]) == int(case['expected_candidate'])
    assert len(field_rows) + len(table_rows) == 52
    details, groups = [], {}
    for case in cases:
        modes = ('column_name', 'column_comment') if case['kind'] == 'column' else ('table_recall',)
        for mode in modes:
            scores = []
            for version, module in (('baseline', old), ('optimized', current)):
                model = sys.modules[module.__package__ + '.models']
                if case['kind'] == 'column':
                    a = model.ColumnInfo(column_name=case['source_name'], column_comment=case['source_comment'] if mode.endswith('comment') else '')
                    b = model.ColumnInfo(column_name=case['target_name'], column_comment=case['target_comment'] if mode.endswith('comment') else '')
                    score = module.column_similarity(a, b)
                    threshold = .75
                else:
                    a = model.TableInfo(table_name=case['source_name'], table_comment=case['source_comment'])
                    b = model.TableInfo(table_name=case['target_name'], table_comment=case['target_comment'])
                    score = module.table_name_similarity(a, b)
                    if version == 'optimized':
                        score = max(score, module.table_name_similarity(b, a))
                    threshold = .60
                prediction = int(score >= threshold)
                groups.setdefault(mode, {}).setdefault(version, []).append((int(case['expected_candidate']), prediction))
                scores.append((score, prediction))
            details.append({**{k: case[k] for k in ('case_id', 'category', 'source_name', 'target_name', 'expected_candidate', 'reason')},
                            'mode': mode, 'baseline_score': scores[0][0], 'baseline_pred': scores[0][1],
                            'optimized_score': scores[1][0], 'optimized_pred': scores[1][1]})
    report = {'baseline_revision': BASELINE, 'input_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (args.csv,args.workbook)},
              'sample_count': len(cases), 'workbook_crosscheck': '52 inputs and labels agree',
              'metrics': {g: {v: metrics(c) for v,c in versions.items()} for g,versions in groups.items()},
              'new_recall': [r for r in details if not r['baseline_pred'] and r['optimized_pred']],
              'new_false_positive': [r for r in details if not r['baseline_pred'] and r['optimized_pred'] and not int(r['expected_candidate'])],
              'regressions': [r for r in details if r['baseline_pred'] and not r['optimized_pred']],
              'ddl_reference': list(workbook.worksheets[3].values),
              'table_scope': 'Name/comment recall only. Labelled table pairs have no field definitions.'}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output/'labelled_metrics.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    with (args.output/'labelled_predictions.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(details[0]))
        writer.writeheader()
        writer.writerows(details)
    print(json.dumps(report['metrics'],ensure_ascii=False))


def synthetic_rows(model):
    rng = random.Random(20261124)
    domains = ['oil','gauge','alarm','defect','voltage','current','temperature','humidity',
               'pressure','vibration','insulation','ground','wind','rain','lightning',
               'fire','gas','cable','breaker','transformer','tower','switch','battery',
               'load','trip','inspection','maintenance','sensor','ledger','quality']
    fields = ['device_code','event_time','alarm_count_7d','alarm_cnt_30d','measure_value',
              'threshold_value','status_code','source_code','region_code','update_time',
              'record_num','failure_rate']
    rows=[]
    for domain in domains:
        for version in range(20):
            table = f'dwd_ecms_{domain}_v{version:02}_di'
            for name in fields[:rng.randint(8,12)]:
                rows.append(model.MetadataRow('zz', table, '', name, 'STRING', ''))
    return rows


def benchmark(args):
    from table_column_same_graph import similarity as current
    module = baseline_module() if args.variant == 'baseline' else current
    model = sys.modules[module.__package__ + '.models']
    rows=synthetic_rows(model)
    count = 0
    if args.variant == 'baseline':
        original=module.table_field_similarity
        def counted(*a, **kw):
            nonlocal count
            count += 1
            return original(*a, **kw)
        context=patch.object(module,'table_field_similarity',counted)
        call=lambda: module.find_all_table_matches(rows)
    else:
        original=module._field_coverages
        def counted(*a, **kw):
            nonlocal count
            count += 1
            return original(*a, **kw)
        context=patch.object(module,'_field_coverages',counted)
        call=lambda: module._find_all_table_matches(rows,use_cache=args.variant!='uncached',bidirectional=args.variant!='forward')
    if args.memory:
        tracemalloc.start()
    with context:
        start=time.perf_counter()
        matches=call()
        elapsed=time.perf_counter()-start
    peak=tracemalloc.get_traced_memory()[1] if args.memory else None
    if args.memory:
        tracemalloc.stop()
    result={'variant':args.variant,'seed':20261124,'physical_tables':600,'field_rows':len(rows),
            'possible_table_pairs':179700,'seconds':elapsed,'tracemalloc_enabled':args.memory,
            'peak_python_mib':peak/(1024**2) if peak is not None else None,
            'field_stage_pairs':count,'rule_candidates':len(matches),'python':platform.python_version(),
            'platform':platform.platform(),'baseline_revision':BASELINE}
    args.output.mkdir(parents=True,exist_ok=True)
    suffix='_memory' if args.memory else ''
    (args.output/f'benchmark_{args.variant}{suffix}.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result),flush=True)


def all_column_scan(args):
    """Scan every field pair, without allocating or sorting candidate objects."""
    from table_column_same_graph import models, similarity
    from table_column_same_graph.config import SIMILARITY_CONFIG
    rows = synthetic_rows(models)
    tracemalloc.start()
    start = time.perf_counter()
    fields = [similarity._Field.prepare(row) for row in rows]
    candidate_count = 0
    for left, right in combinations(fields, 2):
        score = similarity.feature_similarity(left.tokens, right.tokens)
        candidate_count += score >= SIMILARITY_CONFIG.all_column_threshold
    elapsed = time.perf_counter() - start
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    result = {'physical_tables': 600, 'field_rows': len(rows),
              'field_pairs_scanned': len(rows)*(len(rows)-1)//2,
              'candidate_count': candidate_count, 'seconds_with_tracemalloc': elapsed,
              'peak_python_mib': peak/(1024**2),
              'scope': 'All pairs scored on the complete dataset. Candidate objects and sorting excluded. Normal all-column entry point not benchmarked.'}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output/'all_column_scan.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result), flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv',type=Path)
    parser.add_argument('--workbook',type=Path)
    parser.add_argument('--output',type=Path,default=ROOT/'validation'/'rules_20261124')
    parser.add_argument('--variant',choices=['baseline','optimized','uncached','forward'])
    parser.add_argument('--memory',action='store_true')
    parser.add_argument('--all-column-scan',action='store_true')
    args=parser.parse_args()
    if args.all_column_scan:
        all_column_scan(args)
    elif args.variant:
        benchmark(args)
    else:
        if args.csv is None or args.workbook is None:
            parser.error('Labelled evaluation requires --csv and --workbook')
        evaluate(args)

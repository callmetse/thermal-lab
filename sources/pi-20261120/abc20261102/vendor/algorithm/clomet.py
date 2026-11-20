"""Auditable table lookup; physical estimates, never RGB-derived measurements."""
import json
from pathlib import Path

SOURCE = 'pythermalcomfort-tables-20261026-secondary'


def parse_json_reply(text):
    text=text.strip()
    if text.startswith('```') and text.endswith('```'):
        text=text.split('\n',1)[1].rsplit('```',1)[0].strip()
    try:
        value=json.loads(text)
        return value if isinstance(value,dict) else None
    except (ValueError,TypeError):
        return None

# Explicit, provisional semantic mappings, not manufacturer/material measurements.
GARMENT_CANDIDATES = {
    'shirt, blouse': ['Sleeveless scoop-neck blouse','Short-sleeve dress shirt',
                     'Long-sleeve dress shirt','Long-sleeve flannel shirt'],
    'top, t-shirt, sweatshirt': ['T-shirt','Short-sleeve knit shirt','Long-sleeve sweat shirt'],
    'pants': ['Thin trousers','Thick trousers','Sweatpants'],
    'shorts': ['Short shorts','Walking shorts'],
    'skirt': ['Thin skirt','Thick skirt'],
    'dress': ['Sleeveless short gown (thin)','Short-sleeve shirt dress',
              'Long-sleeve shirt dress (thin)','Long-sleeve shirt dress (thick)'],
    'sock': ['Ankle socks','Calf length socks','Knee socks (thick)'],
    'shoe': ['Shoes or sandals'],
    'tights, stockings': ['Panty hose'],
    'vest': ['Sleeveless vest (thin)','Sleeveless vest (thick)'],
}


def visible_clothing_candidates(garments, tables):
    rows=[];unmapped=[]
    ambiguous=sorted({g['category'] for g in garments if g.get('ambiguous_with')})
    for category in sorted({g['category'] for g in garments if not g.get('ambiguous_with')}):
        candidates=GARMENT_CANDIDATES.get(category)
        if not candidates:
            unmapped.append(category);continue
        values={name:tables['clo_individual_garments'][name] for name in candidates}
        rows.append({'detected_category':category,'reference_candidates':values,
                     'clo_candidate_min':min(values.values()),'clo_candidate_max':max(values.values())})
    return {'source':SOURCE,'semantic_mapping_version':'provisional-category-map-v1',
            'items':rows,'unmapped_categories':unmapped,'ambiguous_categories_excluded':ambiguous,
            'visible_sum_if_each_category_once':
                [round(sum(x['clo_candidate_min'] for x in rows),3),
                 round(sum(x['clo_candidate_max'] for x in rows),3)] if rows else None,
            'scope':'visible mapped items only; not total ensemble clo, not a confidence interval',
            'unknowns':['hidden layers','material/thickness','unmapped garments','chair effects']}


def reference_context(evidence, tables):
    clothes = evidence.get('clothing', {})
    activity = evidence.get('activity', {})
    ensemble = clothes.get('confirmed_ensemble')
    task = activity.get('confirmed_task')
    return {
        'source': SOURCE,
        'ensemble_clo': {ensemble: tables['clo_typical_ensembles'][ensemble]}
        if ensemble in tables['clo_typical_ensembles'] else {},
        'task_met': {task: tables['met_typical_tasks'][task]}
        if task in tables['met_typical_tasks'] else {},
    }


def estimate(evidence, tables):
    clothes = evidence.get('clothing', {})
    activity = evidence.get('activity', {})
    context = reference_context(evidence, tables)
    valid = evidence.get('observation_valid', True)
    clo = next(iter(context['ensemble_clo'].values()), None) if valid and clothes.get('complete') else None
    met = next(iter(context['task_met'].values()), None) if valid and activity.get('context_verified') else None
    missing = []
    if clo is None: missing.append('complete_clothing_ensemble')
    if met is None: missing.append('verified_activity_context')
    return {'clo': clo, 'met': met,
            'clo_status': 'table_estimate' if clo is not None else 'unknown',
            'met_status': 'table_estimate' if met is not None else 'unknown',
            'missing': missing, 'source': SOURCE}


def validate_draft(draft, evidence, tables):
    """Keep the LLM output separate; never quietly report fallback as model success."""
    expected = estimate(evidence, tables)
    return {'accepted': isinstance(draft, dict) and draft == expected,
            'llm_draft': draft, 'validated_estimate': expected,
            'policy': 'deterministic_table_guard',
            'measurement_status': 'estimated_not_measured'}


def make_messages(evidence, tables):
    return [
        {'role': 'system', 'content':
         'Return only JSON with keys clo, met, clo_status, met_status, missing, source. '
         'Use supplied reference values only. If observation_valid is false, both are null. '
         'clo requires clothing.complete=true and a known confirmed_ensemble. '
         'met requires activity.context_verified=true and a known confirmed_task. '
         'Statuses are table_estimate or unknown. missing lists complete_clothing_ensemble '
         'then verified_activity_context when absent. Never infer activity from a single pose. '
         'source is pythermalcomfort-tables-20261026-secondary.'},
        {'role': 'user', 'content': json.dumps(
            {'evidence': evidence, 'reference': reference_context(evidence, tables)},
            separators=(',', ':'))},
    ]


if __name__ == '__main__':
    tables = json.loads((Path(__file__).parent/'reference_tables.json').read_text())
    evidence = {'observation_valid': True,
                'clothing': {'confirmed_ensemble': None, 'complete': False},
                'activity': {'confirmed_task': None, 'context_verified': False}}
    print(json.dumps(estimate(evidence,tables),indent=2))

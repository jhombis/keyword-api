"""
Google Ads Keyword Planner API — Flask microservice
Deploy on Render.com (free tier) to provide gRPC-based keyword data.
"""

import os
import sys
import json
from flask import Flask, request, jsonify

app = Flask(__name__)

# ── Config from environment variables ────────────────────────────────────────

GOOGLE_ADS_CONFIG = {
    'developer_token': os.environ.get('GOOGLE_ADS_DEVELOPER_TOKEN', ''),
    'client_id': os.environ.get('GOOGLE_ADS_CLIENT_ID', ''),
    'client_secret': os.environ.get('GOOGLE_ADS_CLIENT_SECRET', ''),
    'refresh_token': os.environ.get('GOOGLE_ADS_REFRESH_TOKEN', ''),
    'use_proto_plus': True
}
CUSTOMER_ID = os.environ.get('GOOGLE_ADS_CUSTOMER_ID', '')

# Initialize Google Ads client
from google.ads.googleads.client import GoogleAdsClient
_ads_client = GoogleAdsClient.load_from_dict(GOOGLE_ADS_CONFIG)

# Cache: "City, ST" → geo target resource name
_geo_cache = {}

STATE_NAMES = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
    'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
    'DC': 'District of Columbia', 'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii',
    'ID': 'Idaho', 'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa',
    'KS': 'Kansas', 'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine',
    'MD': 'Maryland', 'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota',
    'MS': 'Mississippi', 'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska',
    'NV': 'Nevada', 'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico',
    'NY': 'New York', 'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio',
    'OK': 'Oklahoma', 'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island',
    'SC': 'South Carolina', 'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas',
    'UT': 'Utah', 'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington',
    'WV': 'West Virginia', 'WI': 'Wisconsin', 'WY': 'Wyoming'
}


def lookup_geo_targets(cities):
    geo_service = _ads_client.get_service('GeoTargetConstantService')
    results = []

    for city in cities:
        cache_key = f"{city['name']}, {city['state']}"
        if cache_key in _geo_cache:
            results.append(_geo_cache[cache_key])
            continue

        state_full = STATE_NAMES.get(city['state'], city['state'])
        query = f"{city['name']}, {state_full}"

        req = _ads_client.get_type('SuggestGeoTargetConstantsRequest')
        req.locale = 'en'
        req.country_code = 'US'
        req.location_names.names.append(query)

        try:
            response = geo_service.suggest_geo_target_constants(request=req)
            for suggestion in response.geo_target_constant_suggestions:
                gtc = suggestion.geo_target_constant
                if gtc.target_type in ('City', 'DMA Region', 'Municipality'):
                    resource = gtc.resource_name
                    _geo_cache[cache_key] = resource
                    results.append(resource)
                    print(f"[Geo] {query} -> {gtc.name} ({gtc.target_type})", file=sys.stderr)
                    break
        except Exception as e:
            print(f"[Geo] Failed: {query}: {e}", file=sys.stderr)

    return results


def lookup_state_geo(cities):
    """Geo target for the first city's STATE — used as the CPC fallback tier."""
    if not cities:
        return None
    state_abbr = cities[0].get('state', '')
    state_full = STATE_NAMES.get(state_abbr)
    if not state_full:
        return None
    cache_key = f"STATE:{state_abbr}"
    if cache_key in _geo_cache:
        return [_geo_cache[cache_key]]

    geo_service = _ads_client.get_service('GeoTargetConstantService')
    req = _ads_client.get_type('SuggestGeoTargetConstantsRequest')
    req.locale = 'en'
    req.country_code = 'US'
    req.location_names.names.append(state_full)
    try:
        response = geo_service.suggest_geo_target_constants(request=req)
        for suggestion in response.geo_target_constant_suggestions:
            gtc = suggestion.geo_target_constant
            if gtc.target_type == 'State':
                _geo_cache[cache_key] = gtc.resource_name
                print(f"[Geo] State fallback: {state_full} -> {gtc.resource_name}", file=sys.stderr)
                return [gtc.resource_name]
    except Exception as e:
        print(f"[Geo] State lookup failed: {state_full}: {e}", file=sys.stderr)
    return None


def fetch_keyword_ideas(keywords, geo_targets=None):
    kp_service = _ads_client.get_service('KeywordPlanIdeaService')

    req = _ads_client.get_type('GenerateKeywordIdeasRequest')
    req.customer_id = CUSTOMER_ID
    req.language = 'languageConstants/1000'

    for geo in (geo_targets or ['geoTargetConstants/2840']):
        req.geo_target_constants.append(geo)

    req.keyword_plan_network = _ads_client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH
    req.keyword_seed.keywords.extend(keywords)

    response = kp_service.generate_keyword_ideas(request=req)

    parsed = []
    for idea in response:
        m = idea.keyword_idea_metrics
        high_bid = m.high_top_of_page_bid_micros or 0
        low_bid = m.low_top_of_page_bid_micros or 0
        cpc_low = round(low_bid / 1_000_000, 2) if low_bid else 0
        cpc_high = round(high_bid / 1_000_000, 2) if high_bid else 0

        volumes = [{'year': v.year, 'month': v.month, 'searches': v.monthly_searches}
                   for v in m.monthly_search_volumes]

        three_mo_change = None
        if len(volumes) >= 6:
            recent_3 = sum(v['searches'] for v in volumes[-3:])
            prev_3 = sum(v['searches'] for v in volumes[-6:-3])
            if prev_3 > 0:
                three_mo_change = round(((recent_3 - prev_3) / prev_3) * 100)

        parsed.append({
            'keyword': idea.text,
            'monthly_searches': m.avg_monthly_searches,
            'cpc_low': cpc_low,
            'cpc_high': cpc_high,
            'competition': m.competition.name,
            'competition_index': m.competition_index or 0,
            'trend': [v['searches'] for v in volumes],
            'three_mo_change': three_mo_change
        })

    parsed.sort(key=lambda x: x['monthly_searches'], reverse=True)
    return parsed


# ── CORS ─────────────────────────────────────────────────────────────────────

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route('/api/keywords', methods=['OPTIONS'])
def keywords_options():
    return '', 200


@app.route('/api/keywords', methods=['POST'])
def keywords():
    try:
        body = request.get_json() or {}
        kws = body.get('keywords', [])
        if not kws:
            return jsonify({'error': 'No keywords provided'}), 400

        cities = body.get('cities', [])
        geo = lookup_geo_targets(cities) if cities else None
        if not geo:
            geo = ['geoTargetConstants/2840']

        # Caller-configurable result size (default 40, hard cap 100)
        try:
            limit = int(body.get('limit', 40))
        except (TypeError, ValueError):
            limit = 40
        limit = max(1, min(limit, 100))

        result = fetch_keyword_ideas(kws, geo)
        result = result[:limit]

        # City-level results often lack bid data (thin local auction volume →
        # Google returns no top-of-page bids). Fill missing CPCs from a
        # state-level pass so the client can still price those keywords.
        if any(not k['cpc_high'] for k in result) and cities:
            broad_geo = lookup_state_geo(cities) or ['geoTargetConstants/2840']
            try:
                broad = fetch_keyword_ideas(kws, broad_geo)
                cpc_map = {b['keyword']: b for b in broad if b['cpc_high']}
                filled = 0
                for k in result:
                    b = cpc_map.get(k['keyword'])
                    if not k['cpc_high'] and b:
                        k['cpc_low'] = b['cpc_low']
                        k['cpc_high'] = b['cpc_high']
                        k['cpc_source'] = 'state'   # flag: bid data from state level
                        filled += 1
                print(f"[CPC fallback] filled {filled} keywords from state level", file=sys.stderr)
            except Exception as e:
                print(f"[CPC fallback] failed: {e}", file=sys.stderr)

        return jsonify({'keywords': result})

    except Exception as e:
        print(f"[Error] {type(e).__name__}: {e}", file=sys.stderr)
        return jsonify({'error': str(e)}), 500


@app.route('/api/forecast', methods=['OPTIONS'])
def forecast_options():
    return '', 200


@app.route('/api/forecast', methods=['POST'])
def forecast():
    """Keyword Planner 'Get search volume and forecasts' equivalent.

    Body: {
      keywords:  ["...", ...],
      cities:    [{name, state}, ...],
      match_type: "PHRASE" | "BROAD" | "EXACT"        (default PHRASE)
      strategy:   "maximize_conversions" | "maximize_clicks" | "manual_cpc"
      daily_budget: USD number   (used by maximize_* strategies)
      max_cpc:      USD number   (used by manual_cpc)
    }
    Returns campaign-level forecast for the next full month.
    """
    try:
        from datetime import date, timedelta

        body = request.get_json() or {}
        kws = body.get('keywords', [])
        if not kws:
            return jsonify({'error': 'No keywords provided'}), 400

        cities = body.get('cities', [])
        geo = lookup_geo_targets(cities) if cities else None
        if not geo:
            geo = ['geoTargetConstants/2840']

        match_name = (body.get('match_type') or 'PHRASE').upper()
        if match_name not in ('PHRASE', 'BROAD', 'EXACT'):
            match_name = 'PHRASE'
        strategy = (body.get('strategy') or 'maximize_conversions').lower()
        daily_budget = float(body.get('daily_budget') or 30)
        max_cpc = float(body.get('max_cpc') or 10)

        kp_service = _ads_client.get_service('KeywordPlanIdeaService')
        req = _ads_client.get_type('GenerateKeywordForecastMetricsRequest')
        req.customer_id = CUSTOMER_ID

        camp = req.campaign
        camp.language_constants.append('languageConstants/1000')
        camp.keyword_plan_network = _ads_client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH
        for g in geo:
            mod = _ads_client.get_type('CriterionBidModifier')
            mod.geo_target_constant = g
            camp.geo_modifiers.append(mod)

        if strategy == 'manual_cpc':
            camp.bidding_strategy.manual_cpc_bidding_strategy.max_cpc_bid_micros = int(max_cpc * 1_000_000)
            camp.bidding_strategy.manual_cpc_bidding_strategy.daily_budget_micros = int(daily_budget * 1_000_000)
        elif strategy == 'maximize_clicks':
            camp.bidding_strategy.maximize_clicks_bidding_strategy.daily_target_spend_micros = int(daily_budget * 1_000_000)
        else:
            camp.bidding_strategy.maximize_conversions_bidding_strategy.daily_target_spend_micros = int(daily_budget * 1_000_000)

        match_enum = getattr(_ads_client.enums.KeywordMatchTypeEnum, match_name)
        ag = _ads_client.get_type('ForecastAdGroup')
        for kw in kws:
            bk = _ads_client.get_type('BiddableKeyword')
            bk.keyword.text = kw
            bk.keyword.match_type = match_enum
            if strategy == 'manual_cpc':
                bk.max_cpc_bid_micros = int(max_cpc * 1_000_000)
            ag.biddable_keywords.append(bk)
        camp.ad_groups.append(ag)

        # Forecast period: a FIXED 30-day window starting on the 1st of next
        # month — matches the business convention (client monthly = daily × 30)
        # so costs reconcile exactly regardless of how many days the month has.
        today = date.today()
        first_next = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        last_next = first_next + timedelta(days=29)   # 30 days inclusive
        req.forecast_period.start_date = first_next.isoformat()
        req.forecast_period.end_date = last_next.isoformat()

        resp = kp_service.generate_keyword_forecast_metrics(request=req)
        m = resp.campaign_forecast_metrics

        return jsonify({
            'period': {'start': first_next.isoformat(), 'end': last_next.isoformat()},
            'settings': {'match_type': match_name, 'strategy': strategy,
                         'daily_budget': daily_budget,
                         'max_cpc': max_cpc if strategy == 'manual_cpc' else None,
                         'geo_count': len(geo), 'keyword_count': len(kws)},
            'metrics': {
                'impressions':      round(m.impressions, 1),
                'clicks':           round(m.clicks, 1),
                'cost':             round((m.cost_micros or 0) / 1_000_000, 2),
                'ctr':              m.click_through_rate,          # ratio 0-1
                'avg_cpc':          round((m.average_cpc_micros or 0) / 1_000_000, 2),
                'conversions':      round(m.conversions, 1),
                'conversion_rate':  m.conversion_rate,             # ratio 0-1
                'avg_cpa':          round((m.average_cpa_micros or 0) / 1_000_000, 2)
            }
        })

    except Exception as e:
        print(f"[Forecast Error] {type(e).__name__}: {e}", file=sys.stderr)
        return jsonify({'error': str(e)}), 500


@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'customer_id': CUSTOMER_ID[:4] + '...'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"\n  Keyword API running on port {port}\n")
    app.run(host='0.0.0.0', port=port, debug=True)

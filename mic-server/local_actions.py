"""Small local action registry: interpreted data, validated actions, no generated code."""
import json
import math
import os
import re
import sys
import time
from urllib.parse import quote
from urllib.request import Request, urlopen

PROMPT_VERSION = 'voice-actions-v5-option-grounding'
MODEL = 'qwen3:8b'
MODELS = {'astra': ('codex', 'gpt-6-astra'), 'sol': ('codex', 'gpt-6.1-sol'),
          'terra': ('codex', 'gpt-5.6-terra'), 'luna': ('codex', 'gpt-6-luna'),
          'opus': ('claude', 'claude-opus-4-8'), 'sonnet': ('claude', 'claude-sonnet-5'),
          'fable': ('claude', 'claude-fable-5-1')}
# Operator-selected voice defaults: triforce-memory/docs/config/agent_orchestration.md
# Explicit model guidance for local voice actions.
# These explicit tuples do not change the daemon's provider-wide fallback.
MODEL_EFFORTS = {'astra': 'high', 'sol': 'high', 'terra': 'xhigh', 'luna': 'max',
                 'opus': 'high', 'sonnet': 'high', 'fable': 'high'}
EFFORTS = {'low', 'medium', 'high', 'xhigh', 'max'}
# Spoken host labels are explicit configuration, never fleet defaults.
HOSTS = {name.strip().casefold()
         for name in os.environ.get('MIC_LOCAL_ACTIONS_HOSTS', '').split(',')
         if re.fullmatch(r'[a-z][a-z0-9]*', name.strip(), re.I)}
ALIASES = {'bitcoin': 'BTC-USD', 'btc': 'BTC-USD', 'ethereum': 'ETH-USD', 'ether': 'ETH-USD', 'eth': 'ETH-USD',
           'apple': 'AAPL', 'microsoft': 'MSFT', 'nvidia': 'NVDA', 'tesla': 'TSLA', 'amazon': 'AMZN',
           'google': 'GOOGL', 'alphabet': 'GOOGL', 'meta': 'META', 'netflix': 'NFLX'}
ACTIONS = {
    'spawn_agent': {'description': 'Start one agent with a stated task', 'required': ['model'],
                    'defaults': {'host': os.environ.get('MIC_LOCAL_ACTIONS_DEFAULT_HOST', '').strip().casefold()}},
    'market_quote': {'description': 'Read the latest reported Bitcoin, Ethereum or stock price', 'required': ['assets']},
}
SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'route': {'type': 'string', 'enum': ['spawn_agent', 'market_quote', 'clarify', 'bart']},
    **{key: {'type': 'string'} for key in ['task', 'model', 'effort', 'host', 'reply']},
    'assets': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 4}},
    'required': ['route', 'task', 'model', 'effort', 'host', 'assets', 'reply']}

# A deliberately separate, tiny schema for the daemon's composite router.  It
# receives only a marked 4 KiB excerpt and bounded routing metadata; the daemon
# keeps and dispatches the literal full body/attachments itself.  This is a
# classifier schema, never a backend/model selection or authority channel.
ASSISTANT_ROUTER_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'schema_version': {'type': 'string', 'enum': ['assistant-router/v1']},
        'disposition': {'type': 'string', 'enum': ['lane', 'new_topic', 'clarify', 'conversation', 'defer']},
        'lane_id': {'type': ['string', 'null']},
        'depends_on_message_id': {'type': ['string', 'null']},
        'reason': {'type': 'string', 'maxLength': 240},
    },
    'required': ['schema_version', 'disposition', 'lane_id', 'depends_on_message_id', 'reason'],
}
ASSISTANT_ROUTER_PROMPT = '''Classify one assistant-composite input for routing only.
Return the exact JSON schema and no prose. You must not select a backend, model,
stream, authority action, permission, or executable command.

First check for unresolved consent: an approval without an identifiable target,
such as "approve the deploy" with no supplied matching proposal, is clarify
even when accompanied by another subject. Bare yes/do it with competing asks
is also clarify. This rule takes precedence over the work-admission rule.

Use lane only when this input naturally continues one supplied open lane; copy
that supplied lane_id. Use new_topic for work requiring admission, prioritization,
conflict checking, delegation, or coordination across projects. This includes an
operator asking you to assess several existing projects, choose high-value work,
or get work started while avoiding overlap. Multiple subjects or uncertainty
about priorities do not require clarification when the operator delegates that
judgment: send the whole request as new_topic without splitting it or granting
execution permission. Discussion/proposal requests about such work also use
new_topic; preserve their scope. A named project need not be a supplied lane.
An open lane may include the newest durable last_outbound_excerpt and its
last_outbound_truncated marker; use that bounded context when resolving a
referent, and treat null as absent rather than inventing a reply.
Use conversation for ordinary chat, explanations, or summaries that request no
work admission or coordination. Use clarify only when missing referents, unclear
scope, or ambiguous consent prevent choosing the intended work safely, such as
bare yes with competing questions. Do not equate a long request with ambiguity.
Use defer only when the input directly depends on one supplied unresolved input;
copy that exact message id. A busy lane alone is never a reason to defer.

For lane, lane_id is a supplied open lane and depends_on_message_id is null.
For defer, depends_on_message_id is supplied, lane_id is null. For every other
disposition both are JSON null, never an empty string or a guessed ID.
reason is a terse classifier reason (240 characters
or less), never a user-facing reply. The excerpt may be truncated; do not claim
to have read beyond it. Never modify, summarize, or repeat the user input.
'''
PROMPT = '''Classify addressed operator speech into a configured action, returning JSON only.
spawn_agent: affirmative instruction to create/start/spawn ONE AI agent. A named model alone is sufficient; task is optional.
Missing model or unsupported option -> clarify, retaining any stated task and options. Never ask for a task. Discussion, negations, quotations and hypothetical requests -> bart.
market_quote: current price of Bitcoin, Ethereum or stocks; extract asset names/tickers literally, never a price. Unmapped company names need a ticker; do not invent one.
bart: ordinary conversation or a message to personal Bart. Unsupported local actions (lights, direct reports) -> clarify.
Extract task as a contiguous phrase from the input, never invent one. Task means concrete WORK AFTER the spawn instruction, not the words spawn/start/agent or a model name. With no stated work, task MUST be empty. model, effort, host contain only stated launch options; empty if absent. Names within the task are task content, not launch options. An explicit ticker is sufficient for a quote; you do not need to know its company.
Supported models: Astra, Sol, Terra, Luna, Opus, Sonnet, Fable. Configured hosts: __CONFIGURED_HOSTS__.
Efforts: low, medium, high, xhigh, max. Only model is required. Task is optional. Leave omitted effort/host empty; validated model-guidance defaults fill effort; a host default exists only when explicitly configured.
Missing model gets one short question. Extract known task and options even when route is clarify.
reply is empty except for clarification; at most 240 characters. Never start a reply with a wake phrase.
All fields required; unused strings empty; unused assets empty.
Examples (all omitted option fields in these examples are empty strings):
Spawn me an agent -> route clarify, task empty, model empty, reply "Which model?"
Spawn an Astra agent -> route spawn_agent, model "astra", task "", reply "".
Spawn a Fable agent -> route spawn_agent, model "fable", task "", reply "".
Start an agent to review microphone tests -> route clarify, task "review microphone tests", reply empty.
Start a Sol agent on __EXAMPLE_HOST__ with medium effort to review tests -> spawn_agent, task "review tests", model "sol", host "__EXAMPLE_HOST__", effort "medium".
Start an agent to review the __EXAMPLE_HOST__ and Sol documentation -> clarify, task "review the __EXAMPLE_HOST__ and Sol documentation", model "", effort "", host "".
Do not spawn an agent -> bart.
Bart said spawn an agent yesterday -> bart.
What happens when I say spawn an agent? -> bart.
What is Bitcoin trading at? -> market_quote, assets ["Bitcoin"].
What are Bitcoin and Ethereum worth? -> market_quote, assets ["Bitcoin", "Ethereum"].
Tell me the Apple stock price -> market_quote, assets ["Apple"].
What is ticker F worth? -> market_quote, assets ["F"].
Turn off the lights -> clarify, reply "Lights are not configured yet. I can spawn agents or look up prices."
Tell Bart I am going to lunch -> bart.
'''.replace('__CONFIGURED_HOSTS__', ', '.join(sorted(HOSTS)) or '(none)')\
    .replace('__EXAMPLE_HOST__', next(iter(sorted(HOSTS)), 'configuredhost'))


def normalized(text):
    return ' '.join(re.findall(r'\w+', text.casefold()))


def asset_symbol(asset):
    value = ALIASES.get(asset.casefold().strip(), asset.strip().upper())
    if not re.fullmatch(r'[A-Z][A-Z0-9.-]{0,14}', value):
        raise ValueError('Please give the stock ticker and repeat the full request.')
    return value


SPAWN_HEADER = re.compile(r"^(?:(?:please|can you|could you|would you|i want you to|i would like you to|i want to|let us|lets|let's)\s+)*(?:spawn|start|launch|create|open)\b[^.!?]{0,100}?\bagent\b", re.I)
FILLER = set("please can could would you i want like to let us lets s spawn start launch create open me an a one ai agent with using use model effort on host at for the and default defaults it should do is its task my choose pick run have".split())


def spawn_header(text):
    header = SPAWN_HEADER.match(text.lstrip())
    return header if header and not re.search(r"\b(?:not|never|don.t|two|three|four|multiple|several)\b", header.group(), re.I) else None


def spawn_fields(data, text, previous=None):
    """Ground new fields in this utterance; existing validated fields are immutable."""
    initial = previous is None
    previous = previous or dict(fields={}, sources={}, missing=['model', 'effort', 'host'])
    fields, sources = dict(previous['fields']), dict(previous['sources'])
    missing = set(previous['missing'])
    context = text
    task = data['task'].strip()
    # The model sometimes repeats an effort/model/host as the task. Keep those
    # words in option parsing; never erase explicitly introduced work.
    task_word = normalized(task)
    duplicate_option = task_word in (set(MODELS) | EFFORTS | HOSTS) and any(
        normalized(data[key]) == task_word for key in ('model', 'effort', 'host'))
    if duplicate_option and not re.search(r'\b(?:task|to|for)\b', text, re.I):
        task = ''
    if task and not fields.get('task'):
        header = spawn_header(text) if not previous['sources'] and not previous['fields'] else None
        offset = header.end() if header else 0
        match = re.search(r'(?<!\w)' + re.escape(task) + r'(?!\w)', text[offset:], re.I)
        if not match or not normalized(task) or len(task) > 4000 or normalized(task) in {'something', 'anything', 'a task', 'work', 'agent', 'yes', 'no', 'okay'} | set(MODELS) | EFFORTS | HOSTS:
            raise ValueError('Please give a concrete task in your own words.')
        start, end = offset+match.start(), offset+match.end()
        fields['task'], sources['task'] = text[start:end], dict(text=text, start=start, end=end)
        missing.discard('task')
        context = text[:start]+' '+text[end:]
    elif task:
        # The model may echo a prior field, but cannot silently replace it.
        if task != fields.get('task'):
            raise ValueError('To change the task, start a new request.')
    words = set(re.findall(r"[a-z0-9]+", context.casefold()))
    unknown = words - FILLER - set(MODELS) - EFFORTS - HOSTS
    for key, allowed in [('model', MODELS), ('effort', EFFORTS), ('host', HOSTS)]:
        value = data[key].strip().casefold()
        stated = words & set(allowed)
        # A model echo/default is not an operator option. Only spoken tokens
        # authorize a known value; the source wins over an inference mismatch.
        if value in allowed and value not in stated:
            value = ''
        if key not in missing and (key in sources or not (stated or value)):
            if (stated and stated != {fields.get(key)}) or (value and value != fields.get(key)):
                raise ValueError('To change existing details, start a new request.')
            continue
        if value and value not in allowed:
            value_words = set(re.findall(r'[a-z0-9]+', value))
            if not value_words or not value_words <= words:
                raise ValueError('Please state the missing option explicitly.')
            unknown -= value_words
            fields.pop(key, None)
            sources.pop(key, None)
            missing.add(key)
        elif len(stated) > 1:
            fields.pop(key, None)
            missing.add(key)
        elif stated:
            actual = next(iter(stated))
            if value not in ('', actual):
                raise ValueError('Please state the missing option explicitly.')
            fields[key], sources[key] = actual, dict(text=text, value=actual)
            missing.discard(key)
        elif value:
            raise ValueError('Please state the missing option explicitly.')
    if initial and unknown and spawn_header(context):
        # Unsupported names in the explicit spawn header mean a missing model,
        # even when the local model correctly refuses to name an unknown option.
        header_words = set(re.findall(r'[a-z0-9]+', spawn_header(context).group().casefold()))
        if unknown <= header_words and 'model' not in fields:
            missing.add('model')
            unknown.clear()
    if unknown:
        raise ValueError('Please state supported model, effort and host details.')
    # Only genuinely omitted optional fields get defaults; invalid ones stay missing.
    default_host = os.environ.get('MIC_LOCAL_ACTIONS_DEFAULT_HOST', '').strip().casefold()
    if default_host and default_host not in HOSTS:
        raise ValueError('Unsupported configured voice action host.')
    for key, default in [('effort', ''), ('host', default_host)]:
        if initial and key not in fields and not data[key].strip() and not (words & (EFFORTS if key == 'effort' else HOSTS)):
            if key == 'host' and not default:
                missing.add('host')
            else:
                fields[key] = default
                missing.discard(key)
    fields.setdefault('task', '')
    return dict(fields=fields, sources=sources, missing=[k for k in ('model','effort','host') if k in missing])


def spawn_decision(spawn):
    missing, fields = spawn['missing'], spawn['fields']
    if missing:
        names = ' and '.join(missing)
        return dict(route='clarify', reply=f'Which {names} should I use?', spawn=spawn)
    provider, model = MODELS[fields['model']]
    return dict(route='spawn_agent', task=fields['task'], host=fields['host'], provider=provider,
                model=model, effort=fields['effort'] or MODEL_EFFORTS[fields['model']],
                effort_source='explicit' if fields['effort'] else 'model_guidance_default', sources=spawn['sources'])


def grounded_symbol(asset, text):
    symbol = asset_symbol(asset)
    aliases = [name for name, ticker in ALIASES.items() if ticker == symbol]
    known_terms = aliases + ([symbol] if aliases else [])
    known = any(re.search(r'(?<!\w)'+re.escape(term)+r'(?!\w)', text, re.I) for term in known_terms)
    # Preserve explicit tickers; title/lower-case unknown company names are not symbols.
    explicit = re.search(r'(?<!\w)'+re.escape(symbol)+r'(?!\w)', text)
    marked = re.search(r'\bticker\s+'+re.escape(symbol)+r'(?!\w)', text, re.I)
    if not (known or explicit or marked):
        raise ValueError('Please give the stock ticker and repeat the full request.')
    return symbol


def validate_decision(data, text):
    if not isinstance(data, dict) or set(data) != set(SCHEMA['required']):
        raise ValueError('The local model returned an invalid action. Please repeat the full request.')
    if any(not isinstance(data[k], str) for k in ['route', 'task', 'model', 'effort', 'host', 'reply']):
        raise ValueError('Invalid action fields. Please repeat the full request.')
    if data['route'] not in SCHEMA['properties']['route']['enum'] or len(data['reply']) > 240:
        raise ValueError('Invalid local reply. Please repeat the full request.')
    assets = data['assets']
    if not isinstance(assets, list) or len(assets) > 4 or any(not isinstance(x, str) or len(x) > 40 for x in assets):
        raise ValueError('Please request at most four assets and repeat the full request.')
    route = data['route']
    if route in ('spawn_agent', 'clarify') and spawn_header(text):
        return spawn_decision(spawn_fields(data, text.lstrip()))
    if route == 'spawn_agent':
        raise ValueError('Please give an explicit request to spawn one agent with its task and model.')
    if route == 'market_quote':
        if not assets:
            raise ValueError('Which stock or cryptocurrency? Repeat the full request with its ticker or name.')
        symbols = []
        for asset in assets:
            symbol = grounded_symbol(asset, text)
            if symbol not in symbols:
                symbols.append(symbol)
        return dict(route=route, symbols=symbols)
    if route == 'clarify':
        reply = data['reply'].strip()
        if not reply or re.match(r'^hey\W+bart\b', reply, re.I):
            raise ValueError('Please repeat the complete request with the missing details.')
        if 'repeat' not in reply.casefold():
            reply = reply[:210].rstrip()+' Repeat the full request.'
        return dict(route=route, reply=reply)
    return dict(route='bart', text=text)


def infer(text, prompt, opener=urlopen):
    body = dict(model=MODEL, think=False, stream=False, keep_alive='30m', format=SCHEMA,
                options=dict(temperature=0, num_ctx=4096, num_predict=350),
                messages=[dict(role='system', content=prompt), dict(role='user', content=text)])
    request = Request('http://127.0.0.1:11434/api/chat', data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    with opener(request, timeout=40) as response:
        result = json.load(response)
    return json.loads(result['message']['content'])


def assistant_router_classify(payload, opener=urlopen):
    """Route one daemon excerpt; full operator input never enters this model call."""
    if not isinstance(payload, dict):
        raise ValueError('Assistant router payload must be an object.')
    if payload.get('schema_version') != 'assistant-router/v1':
        raise ValueError('Assistant router schema version is invalid.')
    excerpt = payload.get('body_excerpt')
    if not isinstance(excerpt, str) or len(excerpt) > 4000:
        raise ValueError('Assistant router excerpt is invalid.')
    truncated = payload.get('body_truncated')
    if not isinstance(truncated, bool):
        raise ValueError('Assistant router truncation marker is invalid.')
    original_length = payload.get('original_length')
    if isinstance(original_length, bool) or not isinstance(original_length, int) or original_length < len(excerpt):
        raise ValueError('Assistant router original length is invalid.')
    recent_messages = payload.get('recent_messages', [])
    if not isinstance(recent_messages, list) or len(recent_messages) > 6 or any(not isinstance(item, dict) for item in recent_messages):
        raise ValueError('Assistant router recent message context is invalid.')
    unresolved = payload.get('unresolved_inputs', [])
    if not isinstance(unresolved, list) or any(not isinstance(item, dict) for item in unresolved):
        raise ValueError('Assistant router unresolved context is invalid.')
    if len(unresolved) > 8:
        raise ValueError('Assistant router unresolved context is too large.')
    open_lanes = payload.get('open_lanes', [])
    if not isinstance(open_lanes, list) or len(open_lanes) > 16 or any(not isinstance(item, dict) for item in open_lanes):
        raise ValueError('Assistant router lane context is invalid.')
    for item in (*recent_messages, *unresolved):
        if not isinstance(item.get('message_id'), str) or len(str(item.get('excerpt') or '')) > 512:
            raise ValueError('Assistant router message context entry is invalid.')
    normalized_lanes = []
    for lane in open_lanes:
        if not isinstance(lane.get('lane_id'), str) or len(str(lane.get('summary') or '')) > 512:
            raise ValueError('Assistant router lane context entry is invalid.')
        last_outbound_excerpt = lane.get('last_outbound_excerpt')
        if last_outbound_excerpt is not None and (
            not isinstance(last_outbound_excerpt, str) or len(last_outbound_excerpt) > 384
        ):
            raise ValueError('Assistant router lane outbound context is invalid.')
        last_outbound_truncated = lane.get('last_outbound_truncated', False)
        if not isinstance(last_outbound_truncated, bool):
            raise ValueError('Assistant router lane outbound truncation is invalid.')
        if last_outbound_excerpt is None and last_outbound_truncated:
            raise ValueError('Assistant router lane outbound truncation is invalid.')
        normalized_lanes.append({
            **lane,
            'last_outbound_excerpt': last_outbound_excerpt,
            'last_outbound_truncated': last_outbound_truncated,
        })
    # Older callers may omit the optional nested fields; canonicalize them at
    # the adapter boundary so the model always receives the same wire shape.
    payload = dict(payload)
    payload['open_lanes'] = normalized_lanes
    body = dict(
        model=MODEL, think=False, stream=False, keep_alive='30m', format=ASSISTANT_ROUTER_SCHEMA,
        options=dict(temperature=0, num_ctx=4096, num_predict=160),
        messages=[
            dict(role='system', content=ASSISTANT_ROUTER_PROMPT),
            dict(role='user', content=json.dumps(payload, separators=(',', ':'))),
        ],
    )
    request = Request('http://127.0.0.1:11434/api/chat', data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    with opener(request, timeout=40) as response:
        result = json.load(response)
    decision = json.loads(result['message']['content'])
    required = {'schema_version', 'disposition', 'lane_id', 'depends_on_message_id', 'reason'}
    if not isinstance(decision, dict) or set(decision) != required:
        raise ValueError('Assistant router returned invalid JSON.')
    if decision.get('schema_version') != 'assistant-router/v1':
        raise ValueError('Assistant router returned an invalid schema version.')
    disposition = decision.get('disposition')
    lane_id = decision.get('lane_id')
    dependency = decision.get('depends_on_message_id')
    reason = decision.get('reason')
    if (
        disposition not in {'lane', 'new_topic', 'clarify', 'conversation', 'defer'}
        or lane_id is not None and not isinstance(lane_id, str)
        or dependency is not None and not isinstance(dependency, str)
        or not isinstance(reason, str) or len(reason) > 240
    ):
        raise ValueError('Assistant router returned invalid routing fields.')
    if disposition == 'defer':
        known = {str(item.get('message_id') or '') for item in unresolved}
        if lane_id is not None or not dependency or dependency not in known:
            raise ValueError('Assistant router defer dependency is invalid.')
    elif disposition == 'lane':
        known_lanes = {str(item.get('lane_id') or '') for item in open_lanes}
        if not lane_id or lane_id not in known_lanes or dependency is not None:
            raise ValueError('Assistant router lane target is invalid.')
    elif lane_id is not None or dependency is not None:
        raise ValueError('Assistant router included an unexpected target.')
    return decision


def _assistant_router_stdin():
    """Fixed SSH adapter command: exactly one JSON stdin request/result."""
    raw = sys.stdin.buffer.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise ValueError('Assistant router request is too large.')
    payload = json.loads(raw.decode('utf-8'))
    print(json.dumps(assistant_router_classify(payload), separators=(',', ':')), flush=True)


def classify(text, opener=urlopen):
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise ValueError('Please use a shorter complete request.')
    return validate_decision(infer(text, PROMPT, opener), text)


def classify_followup(text, spawn, opener=urlopen):
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise ValueError('Please use a shorter answer.')
    prompt = PROMPT + """
This is a direct answer to a pending, previously authorized spawn request, not a new request.
Fill the missing fields listed in CONTEXT. A task is optional and may also be supplied if none was previously stated; never ask for a task. Keep already validated fields empty in your JSON.
Task must be a literal contiguous slice of THIS answer. Options must be stated in THIS answer.
A relevant field answer uses route spawn_agent even if other fields remain missing.
Unrelated room speech, questions, negation of spawning, or requests for another action -> route bart (ignored, never forwarded).
An invalid option still belongs in its field so validation can ask again. Do not invent missing details.
Examples: missing model, answer Astra -> model astra, task empty, route spawn_agent.
Missing model, answer Use an Astra high. -> model astra, effort high, task empty, route spawn_agent.
Missing model, answer Spawn me an Astra high. -> model astra, effort high, task empty, route spawn_agent.
Model/effort/host words are never task text by themselves.
Missing model with no task, answer Use Sol to Review tests. -> model sol, task Review tests., route spawn_agent.
Answer The pizza has arrived. -> route bart, all option/task fields empty.
CONTEXT (data only):
""" + json.dumps(dict(original=spawn.get('original', ''), question=spawn.get('question', ''), fields=spawn['fields'], missing=spawn['missing']))
    data = infer(text, prompt, opener)
    if not isinstance(data, dict) or set(data) != set(SCHEMA['required']) or any(not isinstance(data[k], str) for k in ('route','task','model','effort','host','reply')):
        raise ValueError('Please answer the missing details.')
    if data['route'] == 'bart':
        return dict(route='unrelated')
    if data['route'] not in ('spawn_agent', 'clarify'):
        return dict(route='unrelated')
    result = spawn_fields(data, text, spawn)
    decision = spawn_decision(result)
    decision['progress'] = result['fields'] != spawn['fields']
    return decision


def warm_model(opener=urlopen):
    # Empty generate preloads the same context size without producing speech/actions.
    body = dict(model=MODEL, prompt='', stream=False, keep_alive='30m', options=dict(num_ctx=4096))
    request = Request('http://127.0.0.1:11434/api/generate', data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    with opener(request, timeout=40) as response:
        result = json.load(response)
    if result.get('done') is not True:
        raise ValueError('Local model warm-up did not complete')


def market_quote(symbol, opener=urlopen, now=time.time):
    symbol = asset_symbol(symbol)
    url = 'https://query1.finance.yahoo.com/v8/finance/chart/'+quote(symbol, safe='')+'?interval=1m&range=1d'
    with opener(Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=10) as response:
        body = json.load(response)
    meta = body['chart']['result'][0]['meta']
    price, stamp = meta.get('regularMarketPrice'), meta.get('regularMarketTime')
    currency = meta.get('currency')
    kind = meta.get('instrumentType')
    if meta.get('symbol') != symbol or kind not in {'CRYPTOCURRENCY', 'EQUITY', 'ETF'} or not isinstance(currency, str) or not re.fullmatch('[A-Z]{3}', currency):
        raise ValueError(f'The quote identity for {symbol} could not be verified.')
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0 for x in [price, stamp]):
        raise ValueError(f'The quote for {symbol} is incomplete.')
    retrieved = now()
    if not -120 <= retrieved-stamp <= (300 if kind == 'CRYPTOCURRENCY' else 7*86400):
        raise ValueError(f'The latest quote for {symbol} is too old or has an invalid time.')
    name = str(meta.get('shortName') or symbol)[:80]
    spoken_name = {'BTC-USD': 'Bitcoin', 'ETH-USD': 'Ethereum'}.get(symbol, name)
    spoken_currency = 'US dollars' if currency == 'USD' else currency
    qualifier = '' if kind == 'CRYPTOCURRENCY' else ', latest reported'
    speech = f'{spoken_name}: {price:,.2f} {spoken_currency}{qualifier}.'
    return dict(symbol=symbol, name=name, price=price, currency=currency, quote_at=stamp,
                retrieved_at=retrieved, source='Yahoo Finance', source_url=url, speech=speech)


if __name__ == '__main__':
    if len(sys.argv) != 2 or sys.argv[1] != 'assistant-router-stdin':
        raise SystemExit('usage: local_actions.py assistant-router-stdin')
    try:
        _assistant_router_stdin()
    except Exception as exc:
        print(json.dumps({'error': 'assistant_router_failed', 'detail': type(exc).__name__}), file=sys.stderr)
        raise SystemExit(2) from exc

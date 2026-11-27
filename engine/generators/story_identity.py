"""Conservative, deterministic proposal identities; no publication inference or I/O on import."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import unicodedata

# Event-specific aliases, never broad people, places or topic families.
# A different year or substantive residual words keep a separate identity.
_EVENTS = (
    ('flight_19', ('flight 19',), '1945'),
    ('uss_cyclops', ('uss cyclops',), '1918'),
    ('ss_waratah', ('ss waratah',), '1909'),
    ('wow_signal', ('wow signal',), '1977'),
    ('lorimer_burst', ('lorimer burst',), '2007'),
    ('mary_celeste', ('mary celeste',), '1872'),
    ('mv_joyita', ('mv joyita',), '1955'),
    ('tunguska_event', ('tunguska event', 'tunguska explosion', 'tunguska'), '1908'),
    ('beaumont_children', ('beaumont children',), '1966'),
)
_FRAMING = frozenset('the a an mystery mysterious unsolved strange story of what really happened to why never returned disappearance disappeared vanished vanishing explained explaining in about behind enigma'.split())


def normalized_title(text):
    """Remove list formatting and typography, preserving semantic word order/numbers."""
    value = unicodedata.normalize('NFKC', text).casefold().strip()
    value = re.sub(r'^\s*(?:[-*•]\s*|\d+[.)]\s+)', '', value)
    value = value.replace('\u2019', "'")
    value = re.sub(r"(?<=\w)'(?=\w)", '', value)
    return ' '.join(re.findall(r'[^\W_]+', value, re.UNICODE))


def canonical_story_key(text):
    value = normalized_title(text)
    if not value:
        return None
    for key, aliases, year in _EVENTS:
        for alias in aliases:
            match = re.search(r'(?<!\w)' + re.escape(alias) + r'(?!\w)', value)
            if match:
                residual = (value[:match.start()] + ' ' + value[match.end():]).split()
                if all(word in _FRAMING or word == year for word in residual):
                    return key
    # Unknown stories: only strip explicit introductory framing. Do not discard
    # entities, dates, angle words, or reorder tokens to manufacture a match.
    value = re.sub(r'^(?:the )?(?:mystery of|story of) (?:the )?', '', value)
    return 'title:' + value.replace(' ', '_')


def history_keys(memory):
    """Legacy memory is topic -> proposal strings, scoped by its owning channel."""
    return {key for ideas in memory.values() for idea in ideas
            if (key := canonical_story_key(idea)) is not None}


def cross_channel_overlaps(memories):
    """Explicit input only. Proposal overlap does not establish repeated publication."""
    grouped = defaultdict(lambda: defaultdict(set))
    for channel, memory in memories.items():
        for ideas in memory.values():
            for idea in ideas:
                key = canonical_story_key(idea)
                if key is not None:
                    grouped[key][channel].add(idea)
    return [{'canonical_story_key': key, 'evidence_type': 'idea_proposals',
             'published_status': 'unknown',
             'channels': {channel: sorted(titles) for channel, titles in sorted(channels.items())}}
            for key, channels in sorted(grouped.items()) if len(channels) > 1]


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only cross-channel proposal overlap report')
    parser.add_argument('--channel-memory', action='append', required=True, metavar='CHANNEL=PATH')
    args = parser.parse_args(argv)
    memories = {}
    for spec in args.channel_memory:
        channel, separator, path = spec.partition('=')
        if not separator or not channel or channel in memories:
            parser.error('Provide unique CHANNEL=PATH inputs')
        memory = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if not isinstance(memory, dict) or any(not isinstance(v, list) or
                any(not isinstance(item, str) for item in v) for v in memory.values()):
            parser.error('Memory must contain topic lists of proposal strings')
        memories[channel] = memory
    print(json.dumps({'scope': 'supplied_channel_memories_only',
                      'overlaps': cross_channel_overlaps(memories)}, indent=2, ensure_ascii=True))


if __name__ == '__main__':
    main()

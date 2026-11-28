"""Opt-in format profiles; the registered Shorts configuration stays unchanged."""
from config.channels import ChannelConfig


class LongformChannel(ChannelConfig):
    @property
    def content_format(self):
        return 'longform'


LONGFORM_GUIDANCE = {
    'minute_mysteries': (
        'Choose one documented mystery with enough evidence and competing explanations for a standalone deep story: '
        'unexplained disappearances, maritime or aviation mysteries, strange signals, historical mysteries, lost '
        'expeditions or unusual documented incidents. Structure: strong opening hook; essential background; '
        'chronological story; evidence and clues; credible explanations; unresolved uncertainty; concise conclusion. '
        'No unsupported conspiracy, invented clues or speculation presented as fact.'),
    'space_science': (
        'Choose one deep scientific question about black holes, planets, unusual astronomical objects, cosmic '
        'signals, space missions, discoveries, planetary phenomena or strange observations. Structure: scientific '
        'question and hook; context; phenomenon or discovery; scientific explanation; evidence and examples; '
        'what scientists still do not know; conclusion. Do not manufacture mystery where science explains it.'),
    'hidden_history': (
        'True historical stories that are surprising, strange, consequential, forgotten, or difficult to believe, '
        'but well documented. Choose one event with named people, places or objects and significant consequences: '
        'bizarre historical events, forgotten disasters, espionage, strange inventions, abandoned or lost places, '
        'historical disappearances, unusual conflicts or unexpected true incidents. Prefer at least three of: '
        'clear event, named subject, consequence, surprise, conflict, visual potential, factual depth, genuine '
        'curiosity gap. Structure: striking consequence or event hook; historical context; story; turning point; '
        'outcome; why it mattered; conclusion. Reject textbook overviews, broad biographies, random trivia, dry '
        'chronology and unsupported folklore. Explain known causes; never manufacture mystery.'),
}


def longform_channel(channel):
    """Derive an isolated stream without renaming any existing channel or Short state."""
    if channel.content_format != 'shortform' or channel.channel_key not in LONGFORM_GUIDANCE:
        raise ValueError('Long-form requires one of the three registered Shorts channels')
    data = channel.model_dump()
    data.update(
        publication_slots=('21:00',), preparation_times=('02:00',),
        state_namespace=channel.state_namespace + '-longform',
        artifact_namespace=channel.artifact_namespace + '-longform',
        output_namespace='output/longform/' + channel.channel_key,
        content_instructions=LONGFORM_GUIDANCE[channel.channel_key],
    )
    return LongformChannel.model_validate(data)

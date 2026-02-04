"""Text processing utilities."""

import re


def strip_model_tags(content: str) -> str:
    """Strip reasoning and wrapper tags from model output.
    
    Removes:
    - <think>...</think> blocks (Kimi reasoning)
    - <thinking>...</thinking> blocks (Claude extended thinking)
    - <result>...</result> wrapper (keeps inner content)
    - <|tool_calls_section_begin|> and similar (Kimi tool markers)
    
    Args:
        content: Raw model output
        
    Returns:
        Cleaned content with tags stripped
    """
    if not content:
        return content
    
    # Strip thinking blocks entirely
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
    content = re.sub(r'<thinking>.*?</thinking>', '', content, flags=re.DOTALL)
    
    # Strip result wrapper but keep inner content
    content = re.sub(r'<result>\s*', '', content)
    content = re.sub(r'\s*</result>', '', content)
    
    # Strip Kimi tool call markers (these should be in tool_calls field, not content)
    content = re.sub(r'<\|tool_calls_section_begin\|>.*', '', content, flags=re.DOTALL)
    content = re.sub(r'<\|tool_call_begin\|>.*', '', content, flags=re.DOTALL)
    
    return content.strip()

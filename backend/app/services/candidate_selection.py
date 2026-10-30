"""Interpret numbers only inside a frozen, visible candidate list."""
import re


class SelectionError(ValueError):
    pass


def candidate_indices(text: str, count: int) -> list[int] | None:
    text=' '.join(text.casefold().replace('ё','е').strip(' .!?').split())
    if text in {'все','все найденные','все показанные'}:
        return list(range(count))
    if re.fullmatch(r'-\d+',text):
        raise SelectionError('Выберите номера только из показанного списка.')
    first=re.fullmatch(r'первые (\d+)',text)
    span=re.fullmatch(r'(?:с|от) (\d+) (?:по|до) (\d+)',text) or re.fullmatch(r'(\d+)\s*[–—-]\s*(\d+)',text)
    if first:
        indices=list(range(min(int(first[1]),count+1)))
    elif span:
        left,right=int(span[1]),int(span[2])
        if left>right or right-left>count:
            raise SelectionError('Выберите номера только из показанного списка.')
        indices=list(range(left-1,right))
    elif re.fullmatch(r'\d+(?:\s*(?:,|и)\s*\d+)*',text):
        indices=list(dict.fromkeys(int(n)-1 for n in re.findall(r'\d+',text)))
    else:
        return None
    if not indices or any(i<0 or i>=count for i in indices):
        raise SelectionError('Выберите номера только из показанного списка.')
    return indices

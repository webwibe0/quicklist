"""Real CSV synonym failure: an interface type word must not become a person."""
import pytest

from app.services.search_filters import literal_query
from app.services.search_guard import constraint_assessment


@pytest.mark.parametrize('label',['задачу','встречу','напоминание','событие'])
def test_bare_interface_type_is_removed_only_from_the_search_instruction(label):
    query=literal_query(f'Найди {label} связаться с Сергеем')
    assert query=='связаться с Сергеем'
    assert constraint_assessment(query,{'title':'Позвонить Сергеем','status':'pending'})['state']=='PASS'


def test_explicitly_quoted_title_words_are_not_erased():
    assert 'задачу' in literal_query('Найди «задачу связаться с Сергеем»')


def test_real_unknown_person_and_different_person_still_cannot_pass():
    query=literal_query('Найди задачу связаться с Сергеем')
    assert constraint_assessment(query,{'title':'Позвонить','status':'pending'})['state']=='UNVERIFIED'
    assert constraint_assessment(query,{'title':'Позвонить Ивану','status':'pending'})['state']!='PASS'

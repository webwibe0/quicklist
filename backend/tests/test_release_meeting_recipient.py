from datetime import time

import pytest

from tests.test_manual_ux_regressions import handle, rows
from tests.test_manual_ux_regressions import ux as ux


def test_saved_real_recipient_without_title_only_asks_time(ux):
    response = handle(ux, 'Поставь встречу с Иваном завтра', {
        'intent':'CREATE_EVENT', 'event_type':'MEETING', 'title':None,
        'recipient_expression':'Иваном', 'date_expression':'завтра',
    })
    assert response.requires_input and response.missing_fields == ['time']
    assert rows(ux) == []
    response = handle(ux, 'В 15:00', {'intent':'CREATE_EVENT','time_expression':'В 15:00'})
    assert not response.requires_input
    assert len(rows(ux)) == 1
    assert rows(ux)[0].title == 'Встреча с Иваном'
    assert rows(ux)[0].event_time == time(15)


@pytest.mark.parametrize('text,kind,recipient', [
    ('Поставь встречу с ним завтра','MEETING','ним'),
    ('Напомни Ивану завтра','REMINDER','Ивану'),
    ('Поставь завтра','MEETING','Иваном'),
])
def test_missing_content_or_unresolved_recipient_does_not_create(ux,text,kind,recipient):
    if recipient == 'Иваном':
        # No invented entity is passed through provenance validation.
        recipient = None
    response = handle(ux,text,{'intent':'CREATE_EVENT','event_type':kind,'title':None,
                             'recipient_expression':recipient,'date_expression':'завтра'})
    assert response.requires_input and 'title' in response.missing_fields
    assert rows(ux) == []

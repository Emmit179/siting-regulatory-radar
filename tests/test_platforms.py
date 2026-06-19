from countywatch.platforms import detect_platform, parse_listing


def test_civicplus_agenda_and_minutes_links():
    html = b"""
    <html><head><title>Agenda Center</title></head><body>
      <div class="meeting"><h3>Commissioners Court - June 9, 2026</h3>
       <a href="/AgendaCenter/ViewFile/Agenda/_06092026-123">Agenda</a>
       <a href="/AgendaCenter/ViewFile/Minutes/_06092026-123">Minutes</a>
       <a href="/AgendaCenter/Commissioners-Court-2">View Meeting</a>
      </div>
    </body></html>
    """
    assert detect_platform("https://county.gov/AgendaCenter", html.decode()) == "civicplus"
    docs, details = parse_listing(html, "https://county.gov/AgendaCenter", "civicplus")
    assert {doc.document_type for doc in docs} >= {"agenda", "minutes"}
    assert all(doc.meeting_date == "2026-06-09" for doc in docs)
    assert details


def test_inline_meeting_page_is_document_candidate():
    html = b"""
    <html><head><title>Commissioners Court Agenda</title></head><body>
      <h1>Regular Meeting Agenda - May 4, 2026</h1>
      <p>Agenda Item 8: Discuss utility-scale solar facility setback regulations.</p>
    </body></html>
    """
    docs, _ = parse_listing(html, "https://county.gov/meetings/2026-05-04")
    assert any(doc.url == "https://county.gov/meetings/2026-05-04" for doc in docs)

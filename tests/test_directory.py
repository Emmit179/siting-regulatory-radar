from countywatch.directory import parse_county_profile


def test_parse_official_county_profile():
    html = b"""
    <html><body><h1>Example County</h1>
      <div>County Website <a href="https://www.examplecounty.gov/">Visit</a></div>
      <div>County Seat Exampleville</div><div>Established 1850</div>
      <a href="https://texascountiesdeliver.org/mycounty/">Explore</a>
    </body></html>
    """
    official, seat = parse_county_profile(html, "https://texascountiesdeliver.org/county/example/")
    assert official == "https://www.examplecounty.gov/"
    assert seat == "Exampleville"

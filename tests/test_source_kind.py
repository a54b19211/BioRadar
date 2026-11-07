"""The kind of source BioCheck is told for a record comes from the material kind Fabric stated."""


def test_a_material_kind_fabric_did_not_state_is_read_as_news_whatever_the_source():
    from bioradar.research.run.report import source_kind

    assert source_kind("trial_registry") == "registry" and source_kind("official_notice") == "official"
    assert source_kind(None) == "news" and source_kind("document") == "news"

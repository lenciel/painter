from painter.naming import slugify, work_filename, work_stem, year_component


def test_spaces_become_underscores():
    assert slugify("William Merritt Chase") == "William_Merritt_Chase"
    assert slugify("At the Seaside") == "At_the_Seaside"


def test_whitespace_runs_and_padding():
    assert slugify("  At  the\tSeaside\n") == "At_the_Seaside"


def test_path_hostile_characters_are_dropped():
    assert slugify('Woman/Man: "Study" <1>') == "WomanMan_Study_1"
    assert "/" not in slugify("a/b")


def test_empty_component_falls_back():
    assert slugify("") == "unknown"
    assert slugify(None) == "unknown"
    assert slugify("...") == "unknown"


def test_unicode_is_preserved():
    assert slugify("Bauhausbücher") == "Bauhausbücher"


def test_year_prefers_single_year_in_free_text():
    # "ca. 1892" -> 1892，即使机器可读的起止年是 1889-1892。
    assert year_component("ca. 1892", 1889, 1892) == "1892"
    assert year_component("1928", 1928, 1928) == "1928"


def test_year_uses_machine_range_for_spans():
    assert year_component("1865–67", 1865, 1867) == "1865-1867"
    assert year_component("19th century", 1800, 1899) == "1800-1899"
    assert year_component("", 1850, 1850) == "1850"


def test_year_falls_back_to_text_when_no_dates():
    assert year_component("late 17th century", None, None) == "late_17th_century"


def test_filename_matches_requested_scheme():
    assert (
        work_filename("William Merritt Chase", "At the Seaside", "1892")
        == "William_Merritt_Chase_At_the_Seaside_1892.jpeg"
    )
    assert (
        work_filename("Albert Gleizes", "Kubismus", "1928")
        == "Albert_Gleizes_Kubismus_1928.jpeg"
    )


def test_filename_extension_is_configurable():
    assert work_stem("A B", "C D", "1900") == "A_B_C_D_1900"
    assert work_filename("A B", "C D", "1900", extension="png") == "A_B_C_D_1900.png"


def test_long_titles_stay_within_filesystem_limits():
    name = work_filename("A" * 100, "T" * 200, "1892")
    assert len(name.encode("utf-8")) <= 255
    assert name.endswith(".jpeg")

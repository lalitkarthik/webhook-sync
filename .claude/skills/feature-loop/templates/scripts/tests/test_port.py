# tests/test_port.py
import port
import fl_lib as fl


def test_port_cli_prints_and_records(repo, capsys):
    assert port.main([str(repo)]) == 0
    printed = int(capsys.readouterr().out.strip())
    assert printed == fl.port_for(repo)
    assert (repo / ".verify" / "port").read_text() == str(printed)

"""
adapters/__init__.py
====================
Factory per creare l'adapter giusto dato un tariff_id.

Uso:
    from adapters import get_adapter

    config = {...}  # entry da evu_list.json
    adapter = get_adapter(config)
    prices  = await adapter.fetch(target_date)
"""

from adapters.base import (
    BaseAdapter,
    AdapterError,
    AdapterNetworkError,
    AdapterParseError,
    AdapterEmptyError,
)
from adapters.ckw import CkwAdapter
from adapters.primeo_energie import PrimeoEnergieAdapter
from adapters.aem import AemAdapter
from adapters.ekz import EkzAdapter
from adapters.groupe_e import GroupeEAdapter
from adapters.ail import AilAdapter          # ← aggiungi questa riga

_REGISTRY = {
    "CkwAdapter":           CkwAdapter,
    "PrimeoEnergieAdapter": PrimeoEnergieAdapter,
    "AemAdapter":           AemAdapter,
    "EkzAdapter":           EkzAdapter,
    "GroupeEAdapter":       GroupeEAdapter,
    "AilAdapter":           AilAdapter,      # ← aggiungi questa riga
}

def get_adapter(tariff_config: dict) -> BaseAdapter:
    """
    Crea e ritorna l'adapter corretto per la configurazione data.

    Args:
        tariff_config: entry da evu_list.json

    Returns:
        Istanza dell'adapter corretto

    Raises:
        ValueError: se adapter_class non è registrato
    """
    adapter_class = tariff_config.get("adapter_class", "")
    cls = _REGISTRY.get(adapter_class)

    if cls is None:
        available = ", ".join(_REGISTRY.keys())
        raise ValueError(
            f"Adapter '{adapter_class}' non trovato. "
            f"Disponibili: {available}"
        )

    return cls(tariff_config)


def list_available_adapters() -> list[str]:
    """Lista degli adapter_class implementati."""
    return list(_REGISTRY.keys())
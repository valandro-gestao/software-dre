"""
Classe base para processadores de cliente.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Dict, Any

import pandas as pd


@dataclass
class ResultadoETL:
    lancamentos: List[Dict[str, Any]] = field(default_factory=list)
    inconsistencias: List[Dict[str, Any]] = field(default_factory=list)
    categorias_nao_mapeadas: List[Dict[str, Any]] = field(default_factory=list)
    # Dados extras por processador (ex.: auditoria Zeus multi-CNPJ).
    # Chave "auditoria" → list[dict] gerada como resumo_auditoria.csv.
    metadata: Dict[str, Any] = field(default_factory=dict)


class ProcessadorBase(ABC):
    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def processar(self, df: pd.DataFrame) -> ResultadoETL:
        ...

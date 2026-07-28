import json
import logging
import sqlite3
import threading
from pathlib import Path
from datetime import datetime


class BotDatabase:
    """Camada simples de persistência em SQLite para o bot.

    O código antigo trabalhava com dicionários carregados de arquivos JSON.
    Esta classe mantém uma interface parecida para facilitar a migração sem
    reescrever todo o bot de uma vez, mas grava tudo em tabelas SQLite.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.inicializar()

    def inicializar(self):
        with self._lock, self._conn:
            self._conn.executescript(
                """

                CREATE TABLE IF NOT EXISTS pedidos_pendentes (
                    pedido_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    status TEXT,
                    mp_payment_id TEXT,
                    plataforma_order_id TEXT,
                    dados_json TEXT NOT NULL,
                    criado_em TEXT,
                    atualizado_em TEXT
                );

                CREATE TABLE IF NOT EXISTS pedidos_historico (
                    pedido_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    status TEXT,
                    mp_payment_id TEXT,
                    plataforma_order_id TEXT,
                    dados_json TEXT NOT NULL,
                    criado_em TEXT,
                    atualizado_em TEXT
                );

                CREATE TABLE IF NOT EXISTS comprovantes_usados (
                    file_unique_id TEXT PRIMARY KEY,
                    pedido_id TEXT,
                    user_id TEXT,
                    dados_json TEXT NOT NULL,
                    criado_em TEXT
                );

                CREATE TABLE IF NOT EXISTS pagamentos_processados (
                    payment_id TEXT PRIMARY KEY,
                    pedido_id TEXT,
                    user_id TEXT,
                    dados_json TEXT NOT NULL,
                    processado_em TEXT
                );

                CREATE TABLE IF NOT EXISTS totais_semanais (
                    chave TEXT PRIMARY KEY,
                    dados_json TEXT NOT NULL,
                    atualizado_em TEXT
                );

                CREATE TABLE IF NOT EXISTS webhook_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payment_id TEXT NOT NULL UNIQUE,
                    origem TEXT NOT NULL DEFAULT 'webhook',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pendente',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    criado_em TEXT NOT NULL,
                    atualizado_em TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tickets_suporte (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    usuario_id TEXT NOT NULL,
                    usuario_nome TEXT,
                    usuario_username TEXT,
                    status TEXT NOT NULL DEFAULT 'aberto',
                    atendente_id TEXT,
                    atendente_nome TEXT,
                    criado_em TEXT NOT NULL,
                    assumido_em TEXT,
                    fechado_em TEXT,
                    fechado_por TEXT,
                    dados_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_pedidos_pendentes_status ON pedidos_pendentes(status);
                CREATE INDEX IF NOT EXISTS idx_pedidos_historico_user ON pedidos_historico(user_id);
                CREATE INDEX IF NOT EXISTS idx_webhook_status ON webhook_events(status, attempts, atualizado_em);
                CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets_suporte(status, criado_em);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_ticket_usuario_ativo
                    ON tickets_suporte(usuario_id)
                    WHERE status IN ('aberto', 'em_atendimento');
                CREATE UNIQUE INDEX IF NOT EXISTS idx_ticket_atendente_ativo
                    ON tickets_suporte(atendente_id)
                    WHERE status = 'em_atendimento'
                      AND atendente_id IS NOT NULL
                      AND atendente_id <> '';
                """
            )
            # A versão sem cadastro não mantém registros ou a tabela legada de usuários.
            # O DROP também remove índices associados em bancos existentes.
            self._conn.execute("DROP TABLE IF EXISTS usuarios")

    @staticmethod
    def _dump(dados) -> str:
        return json.dumps(dados or {}, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _load(texto: str):
        try:
            return json.loads(texto or "{}")
        except Exception:
            return {}

    def _load_mapping(self, tabela: str, key_col: str) -> dict:
        with self._lock:
            rows = self._conn.execute(f"SELECT {key_col}, dados_json FROM {tabela}").fetchall()
        return {str(row[key_col]): self._load(row["dados_json"]) for row in rows}

    def _replace_mapping(self, tabela: str, key_col: str, mapping: dict, upsert_func):
        with self._lock, self._conn:
            self._conn.execute(f"DELETE FROM {tabela}")
            for chave, dados in (mapping or {}).items():
                if isinstance(dados, dict):
                    dados.setdefault(key_col if key_col != "file_unique_id" else "file_unique_id", str(chave))
                upsert_func(str(chave), dados, commit=False)




    def carregar_pedidos_pendentes(self) -> dict:
        return self._load_mapping("pedidos_pendentes", "pedido_id")

    def salvar_pedidos_pendentes(self, pedidos: dict):
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM pedidos_pendentes")
            for pedido_id, pedido in (pedidos or {}).items():
                self.salvar_pedido_pendente(pedido_id, pedido, commit=False)

    def salvar_pedido_pendente(self, pedido_id, pedido: dict, commit: bool = True):
        self._salvar_pedido("pedidos_pendentes", pedido_id, pedido, commit)

    def remover_pedido_pendente(self, pedido_id):
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM pedidos_pendentes WHERE pedido_id = ?", (str(pedido_id),))

    def carregar_pedidos_historico(self) -> dict:
        return self._load_mapping("pedidos_historico", "pedido_id")

    def salvar_pedido_historico(self, pedido_id, pedido: dict, commit: bool = True):
        self._salvar_pedido("pedidos_historico", pedido_id, pedido, commit)

    def _salvar_pedido(self, tabela: str, pedido_id, pedido: dict, commit: bool = True):
        pedido_id = str(pedido_id or (pedido or {}).get("pedido_id") or "").strip()
        if not pedido_id:
            return
        pedido = dict(pedido or {})
        pedido["pedido_id"] = pedido_id
        atualizado_em = datetime.now().isoformat(timespec="seconds")
        sql = f"""
            INSERT INTO {tabela}
            (pedido_id, user_id, status, mp_payment_id, plataforma_order_id, dados_json, criado_em, atualizado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pedido_id) DO UPDATE SET
                user_id=excluded.user_id,
                status=excluded.status,
                mp_payment_id=excluded.mp_payment_id,
                plataforma_order_id=excluded.plataforma_order_id,
                dados_json=excluded.dados_json,
                atualizado_em=excluded.atualizado_em
        """
        params = (
            pedido_id,
            str(pedido.get("user_id") or ""),
            pedido.get("status"),
            str(pedido.get("mp_payment_id") or ""),
            str(pedido.get("plataforma_order_id") or ""),
            self._dump(pedido),
            pedido.get("criado_em"),
            atualizado_em,
        )
        if commit:
            with self._lock, self._conn:
                self._conn.execute(sql, params)
        else:
            self._conn.execute(sql, params)

    def carregar_comprovantes_usados(self) -> dict:
        return self._load_mapping("comprovantes_usados", "file_unique_id")

    def salvar_comprovantes_usados(self, dados: dict):
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM comprovantes_usados")
            for file_unique_id, item in (dados or {}).items():
                self.salvar_comprovante_usado(file_unique_id, item, commit=False)

    def salvar_comprovante_usado(self, file_unique_id, dados: dict, commit: bool = True):
        file_unique_id = str(file_unique_id or "").strip()
        if not file_unique_id:
            return
        sql = """
            INSERT INTO comprovantes_usados (file_unique_id, pedido_id, user_id, dados_json, criado_em)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(file_unique_id) DO UPDATE SET
                pedido_id=excluded.pedido_id,
                user_id=excluded.user_id,
                dados_json=excluded.dados_json
        """
        params = (
            file_unique_id,
            str((dados or {}).get("pedido_id") or ""),
            str((dados or {}).get("user_id") or ""),
            self._dump(dados),
            (dados or {}).get("registrado_em") or datetime.now().isoformat(timespec="seconds"),
        )
        if commit:
            with self._lock, self._conn:
                self._conn.execute(sql, params)
        else:
            self._conn.execute(sql, params)

    def carregar_pagamentos_processados(self) -> dict:
        return self._load_mapping("pagamentos_processados", "payment_id")

    def salvar_pagamentos_processados(self, dados: dict):
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM pagamentos_processados")
            for payment_id, item in (dados or {}).items():
                self.salvar_pagamento_processado(payment_id, item, commit=False)

    def salvar_pagamento_processado(self, payment_id, dados: dict, commit: bool = True):
        payment_id = str(payment_id or "").strip()
        if not payment_id:
            return
        sql = """
            INSERT INTO pagamentos_processados (payment_id, pedido_id, user_id, dados_json, processado_em)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(payment_id) DO UPDATE SET
                pedido_id=excluded.pedido_id,
                user_id=excluded.user_id,
                dados_json=excluded.dados_json,
                processado_em=excluded.processado_em
        """
        params = (
            payment_id,
            str((dados or {}).get("pedido_id") or ""),
            str((dados or {}).get("user_id") or ""),
            self._dump(dados),
            (dados or {}).get("processado_em") or datetime.now().isoformat(timespec="seconds"),
        )
        if commit:
            with self._lock, self._conn:
                self._conn.execute(sql, params)
        else:
            self._conn.execute(sql, params)

    def carregar_totais_semanais(self) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT dados_json FROM totais_semanais WHERE chave = 'principal'").fetchone()
        return self._load(row["dados_json"]) if row else None

    def salvar_totais_semanais(self, dados: dict):
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO totais_semanais (chave, dados_json, atualizado_em)
                VALUES ('principal', ?, ?)
                ON CONFLICT(chave) DO UPDATE SET dados_json=excluded.dados_json, atualizado_em=excluded.atualizado_em
                """,
                (self._dump(dados), datetime.now().isoformat(timespec="seconds")),
            )

    def enfileirar_webhook(self, payment_id: str, payload: dict | None = None, origem: str = "webhook"):
        payment_id = str(payment_id or "").strip()
        if not payment_id:
            return
        agora = datetime.now().isoformat(timespec="seconds")
        payload_json = self._dump(payload or {})
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO webhook_events (payment_id, origem, payload_json, status, attempts, criado_em, atualizado_em)
                VALUES (?, ?, ?, 'pendente', 0, ?, ?)
                ON CONFLICT(payment_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    status=CASE WHEN webhook_events.status = 'processado' THEN webhook_events.status ELSE 'pendente' END,
                    atualizado_em=excluded.atualizado_em
                """,
                (payment_id, origem, payload_json, agora, agora),
            )

    def listar_webhooks_pendentes(self, limite: int = 20, max_attempts: int = 8) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM webhook_events
                WHERE status IN ('pendente', 'erro') AND attempts < ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (int(max_attempts), int(limite)),
            ).fetchall()
        return [dict(row) for row in rows]

    def marcar_webhook_processando(self, event_id: int) -> bool:
        agora = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                UPDATE webhook_events
                SET status='processando', attempts=attempts + 1, atualizado_em=?
                WHERE id=? AND status IN ('pendente', 'erro')
                """,
                (agora, int(event_id)),
            )
            return cur.rowcount > 0

    def marcar_webhook_processado(self, event_id: int):
        agora = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE webhook_events SET status='processado', last_error=NULL, atualizado_em=? WHERE id=?",
                (agora, int(event_id)),
            )

    def marcar_webhook_erro(self, event_id: int, erro: str):
        agora = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE webhook_events SET status='erro', last_error=?, atualizado_em=? WHERE id=?",
                (str(erro or "")[:500], agora, int(event_id)),
            )

    def recuperar_webhooks_processando_interrompidos(self) -> int:
        """Libera eventos que ficaram travados como processando após restart."""
        agora = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                UPDATE webhook_events
                SET status='erro',
                    last_error='Processamento interrompido por reinício do servidor. Será reavaliado com trava anti-duplicidade.',
                    atualizado_em=?
                WHERE status='processando'
                """,
                (agora,),
            )
            return cur.rowcount or 0

    def _ticket_dict(self, row) -> dict | None:
        if not row:
            return None
        ticket = dict(row)
        ticket["dados"] = self._load(ticket.pop("dados_json", "{}"))
        return ticket

    def criar_ticket(self, usuario_id, usuario_nome: str = "", usuario_username: str = "") -> tuple[dict, bool]:
        """Cria um ticket ou retorna o ticket ativo que o usuário já possui."""
        usuario_id = str(usuario_id or "").strip()
        if not usuario_id:
            raise ValueError("usuario_id é obrigatório")

        agora = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._conn:
            existente = self._conn.execute(
                """
                SELECT * FROM tickets_suporte
                WHERE usuario_id = ? AND status IN ('aberto', 'em_atendimento')
                ORDER BY id DESC
                LIMIT 1
                """,
                (usuario_id,),
            ).fetchone()
            if existente:
                return self._ticket_dict(existente), False

            try:
                cursor = self._conn.execute(
                    """
                    INSERT INTO tickets_suporte
                    (usuario_id, usuario_nome, usuario_username, status, criado_em, dados_json)
                    VALUES (?, ?, ?, 'aberto', ?, '{}')
                    """,
                    (usuario_id, str(usuario_nome or ""), str(usuario_username or ""), agora),
                )
            except sqlite3.IntegrityError:
                existente = self._conn.execute(
                    """
                    SELECT * FROM tickets_suporte
                    WHERE usuario_id = ? AND status IN ('aberto', 'em_atendimento')
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (usuario_id,),
                ).fetchone()
                if existente:
                    return self._ticket_dict(existente), False
                raise
            row = self._conn.execute(
                "SELECT * FROM tickets_suporte WHERE id = ?",
                (int(cursor.lastrowid),),
            ).fetchone()
        return self._ticket_dict(row), True

    def obter_ticket(self, ticket_id) -> dict | None:
        try:
            ticket_id = int(ticket_id)
        except (TypeError, ValueError):
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tickets_suporte WHERE id = ?",
                (ticket_id,),
            ).fetchone()
        return self._ticket_dict(row)

    def obter_ticket_ativo_usuario(self, usuario_id) -> dict | None:
        usuario_id = str(usuario_id or "").strip()
        if not usuario_id:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM tickets_suporte
                WHERE usuario_id = ? AND status IN ('aberto', 'em_atendimento')
                ORDER BY id DESC
                LIMIT 1
                """,
                (usuario_id,),
            ).fetchone()
        return self._ticket_dict(row)

    def obter_ticket_ativo_atendente(self, atendente_id) -> dict | None:
        atendente_id = str(atendente_id or "").strip()
        if not atendente_id:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM tickets_suporte
                WHERE atendente_id = ? AND status = 'em_atendimento'
                ORDER BY id DESC
                LIMIT 1
                """,
                (atendente_id,),
            ).fetchone()
        return self._ticket_dict(row)

    def listar_tickets_abertos(self, limite: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM tickets_suporte
                WHERE status = 'aberto'
                ORDER BY id ASC
                LIMIT ?
                """,
                (max(1, int(limite)),),
            ).fetchall()
        return [self._ticket_dict(row) for row in rows]

    def atualizar_dados_ticket(self, ticket_id, dados: dict) -> dict | None:
        try:
            ticket_id = int(ticket_id)
        except (TypeError, ValueError):
            return None
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE tickets_suporte SET dados_json = ? WHERE id = ?",
                (self._dump(dados or {}), ticket_id),
            )
            row = self._conn.execute(
                "SELECT * FROM tickets_suporte WHERE id = ?",
                (ticket_id,),
            ).fetchone()
        return self._ticket_dict(row)

    def assumir_ticket(self, ticket_id, atendente_id, atendente_nome: str) -> tuple[dict | None, str]:
        """Assume um ticket de modo atômico e limita cada atendente a um ticket."""
        try:
            ticket_id = int(ticket_id)
        except (TypeError, ValueError):
            return None, "nao_encontrado"
        atendente_id = str(atendente_id or "").strip()
        if not atendente_id:
            return None, "atendente_invalido"

        agora = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM tickets_suporte WHERE id = ?",
                (ticket_id,),
            ).fetchone()
            if not row:
                return None, "nao_encontrado"
            ticket = self._ticket_dict(row)
            if ticket["status"] == "fechado":
                return ticket, "fechado"
            if ticket["status"] == "em_atendimento":
                if str(ticket.get("atendente_id") or "") == atendente_id:
                    return ticket, "ja_assumido_por_voce"
                return ticket, "ja_assumido"

            outro = self._conn.execute(
                """
                SELECT id FROM tickets_suporte
                WHERE atendente_id = ? AND status = 'em_atendimento' AND id <> ?
                LIMIT 1
                """,
                (atendente_id, ticket_id),
            ).fetchone()
            if outro:
                return ticket, "atendente_ocupado"

            try:
                cursor = self._conn.execute(
                    """
                    UPDATE tickets_suporte
                    SET status = 'em_atendimento',
                        atendente_id = ?,
                        atendente_nome = ?,
                        assumido_em = ?
                    WHERE id = ? AND status = 'aberto'
                    """,
                    (atendente_id, str(atendente_nome or ""), agora, ticket_id),
                )
            except sqlite3.IntegrityError:
                return ticket, "atendente_ocupado"
            atualizado = self._conn.execute(
                "SELECT * FROM tickets_suporte WHERE id = ?",
                (ticket_id,),
            ).fetchone()
            if cursor.rowcount != 1:
                ticket_atualizado = self._ticket_dict(atualizado)
                if str((ticket_atualizado or {}).get("atendente_id") or "") == atendente_id:
                    return ticket_atualizado, "ja_assumido_por_voce"
                return ticket_atualizado, "ja_assumido"
        return self._ticket_dict(atualizado), "assumido"

    def fechar_ticket(self, ticket_id, fechado_por: str) -> tuple[dict | None, str]:
        try:
            ticket_id = int(ticket_id)
        except (TypeError, ValueError):
            return None, "nao_encontrado"

        agora = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM tickets_suporte WHERE id = ?",
                (ticket_id,),
            ).fetchone()
            if not row:
                return None, "nao_encontrado"
            ticket = self._ticket_dict(row)
            if ticket["status"] == "fechado":
                return ticket, "ja_fechado"

            cursor = self._conn.execute(
                """
                UPDATE tickets_suporte
                SET status = 'fechado', fechado_em = ?, fechado_por = ?
                WHERE id = ? AND status IN ('aberto', 'em_atendimento')
                """,
                (agora, str(fechado_por or ""), ticket_id),
            )
            atualizado = self._conn.execute(
                "SELECT * FROM tickets_suporte WHERE id = ?",
                (ticket_id,),
            ).fetchone()
            if cursor.rowcount != 1:
                return self._ticket_dict(atualizado), "ja_fechado"
        return self._ticket_dict(atualizado), "fechado"

    def fechar_tickets_usuario(self, usuario_id, fechado_por: str) -> int:
        usuario_id = str(usuario_id or "").strip()
        if not usuario_id:
            return 0
        agora = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE tickets_suporte
                SET status = 'fechado', fechado_em = ?, fechado_por = ?
                WHERE usuario_id = ? AND status IN ('aberto', 'em_atendimento')
                """,
                (agora, str(fechado_por or ""), usuario_id),
            )
        return cursor.rowcount or 0

    def contar(self, tabela: str, where: str = "", params: tuple = ()) -> int:
        query = f"SELECT COUNT(*) AS total FROM {tabela}"
        if where:
            query += " WHERE " + where
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return int(row["total"] if row else 0)

    def migrar_jsons_se_vazio(self, paths: dict):
        """Importa dados JSON antigos apenas quando a tabela correspondente está vazia."""
        migracoes = [
            ("pedidos_pendentes", "pedidos_pendentes", self.salvar_pedidos_pendentes),
            ("pedidos_historico", "pedidos_historico", None),
            ("comprovantes_usados", "comprovantes_usados", self.salvar_comprovantes_usados),
            ("pagamentos_processados", "pagamentos_processados", self.salvar_pagamentos_processados),
        ]
        for tabela, nome, salvar in migracoes:
            if self.contar(tabela) > 0:
                continue
            path = paths.get(nome)
            if not path or not Path(path).exists():
                continue
            try:
                dados = json.loads(Path(path).read_text(encoding="utf-8") or "{}")
            except Exception as exc:
                logging.warning("Não foi possível migrar %s para SQLite: %s", path, exc)
                continue
            if not isinstance(dados, dict) or not dados:
                continue
            if tabela == "pedidos_historico":
                with self._lock, self._conn:
                    for pedido_id, pedido in dados.items():
                        self.salvar_pedido_historico(pedido_id, pedido, commit=False)
            else:
                salvar(dados)
            logging.info("Migrado para SQLite: %s (%s registros)", nome, len(dados))

        if self.carregar_totais_semanais() is None:
            path = paths.get("totais_semanais")
            if path and Path(path).exists():
                try:
                    dados = json.loads(Path(path).read_text(encoding="utf-8") or "{}")
                    if isinstance(dados, dict) and dados:
                        self.salvar_totais_semanais(dados)
                        logging.info("Migrado para SQLite: totais_semanais")
                except Exception as exc:
                    logging.warning("Não foi possível migrar totais_semanais para SQLite: %s", exc)

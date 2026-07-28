import os
import re
from urllib.parse import urlparse, urlunparse

import requests


EMAIL_RE = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE)
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9._]{2,30}$")


def validar_email(texto: str) -> tuple[bool, str, str]:
    email = (texto or "").strip().lower()
    if not EMAIL_RE.fullmatch(email):
        return False, email, "⚠️ Envie um e-mail válido. Exemplo: cliente@email.com"
    return True, email, ""


def _dominio(hostname: str) -> str:
    host = (hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _limpar_url(texto: str) -> str:
    texto = (texto or "").strip()
    texto = texto.split()[0] if texto.split() else texto
    return texto.rstrip(".,;)")


def _url_com_scheme(texto: str) -> str:
    if re.match(r"^https?://", texto, re.IGNORECASE):
        return texto
    if "." in texto and not texto.startswith("@"):
        return "https://" + texto
    return texto


def validar_instagram(texto: str) -> tuple[bool, str, str]:
    bruto = _limpar_url(texto)
    if not bruto:
        return False, bruto, "⚠️ Envie o @ ou link do Instagram. Exemplo: @usuario ou https://instagram.com/usuario"

    if bruto.startswith("@") or (USERNAME_RE.fullmatch(bruto) and "." not in bruto):
        usuario = bruto if bruto.startswith("@") else f"@{bruto}"
        return True, usuario, ""

    url = _url_com_scheme(bruto)
    parsed = urlparse(url)
    host = _dominio(parsed.hostname or "")
    if host not in {"instagram.com", "m.instagram.com", "instagr.am", "threads.net"}:
        return False, bruto, "⚠️ Esse link não parece ser do Instagram. Envie @usuario ou link instagram.com/..."
    if not parsed.path or parsed.path.strip("/") == "":
        return False, bruto, "⚠️ O link do Instagram precisa apontar para perfil, post, reels ou story."
    return True, url, ""


def validar_tiktok(texto: str) -> tuple[bool, str, str]:
    bruto = _limpar_url(texto)
    if not bruto:
        return False, bruto, "⚠️ Envie o @ ou link do TikTok. Exemplo: @usuario ou https://tiktok.com/@usuario"

    if bruto.startswith("@") or (USERNAME_RE.fullmatch(bruto) and "." not in bruto):
        usuario = bruto if bruto.startswith("@") else f"@{bruto}"
        return True, usuario, ""

    url = _url_com_scheme(bruto)
    parsed = urlparse(url)
    host = _dominio(parsed.hostname or "")
    dominios = {"tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"}
    if host not in dominios:
        return False, bruto, "⚠️ Esse link não parece ser do TikTok. Envie @usuario ou link tiktok.com/..."
    if host in {"tiktok.com", "m.tiktok.com"} and (not parsed.path or parsed.path.strip("/") == ""):
        return False, bruto, "⚠️ O link do TikTok precisa apontar para perfil ou vídeo."
    return True, url, ""


def _servico_pede_publicacao(pedido: dict) -> bool:
    """True para serviços que só podem receber link de publicação/vídeo.

    Curtidas e visualizações não devem aceitar @ nem link de perfil.
    """
    servico = str((pedido or {}).get("servico_chave") or (pedido or {}).get("servico") or "").lower()
    servico = (
        servico.replace("ç", "c")
        .replace("õ", "o")
        .replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
    )
    return any(p in servico for p in ("curtida", "visualizacao", "visualizac", "view", "like"))


def _servico_pede_perfil(pedido: dict) -> bool:
    """True para serviços que só podem receber perfil.

    Seguidores não devem aceitar links de publicação, Reel, vídeo ou story.
    """
    servico = str((pedido or {}).get("servico_chave") or (pedido or {}).get("servico") or "").lower()
    servico = (
        servico.replace("ç", "c")
        .replace("õ", "o")
        .replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
    )
    return any(p in servico for p in ("seguidor", "followers", "follower"))


def validar_instagram_perfil(texto: str) -> tuple[bool, str, str]:
    bruto = _limpar_url(texto)
    if not bruto:
        return False, bruto, (
            "⚠️ Para seguidores no Instagram, envie somente o @ ou link do perfil.\n\n"
            "Não envie link de publicação/Reel/vídeo."
        )

    if bruto.startswith("@") or (USERNAME_RE.fullmatch(bruto) and "." not in bruto):
        usuario = bruto if bruto.startswith("@") else f"@{bruto}"
        return True, usuario, ""

    ok, url, erro = validar_instagram(bruto)
    if not ok:
        return ok, url, erro

    if _instagram_eh_publicacao(url):
        return False, url, (
            "⚠️ Para seguidores no Instagram, envie somente o @ ou link do perfil.\n\n"
            "Link de publicação/Reel/vídeo não é aceito nesse serviço."
        )

    if not _instagram_username_de_url(url):
        return False, url, (
            "⚠️ Para seguidores no Instagram, envie um @ ou link de perfil válido.\n\n"
            "Exemplo: @usuario ou https://www.instagram.com/usuario/"
        )

    return True, _normalizar_url_social(url), ""


def validar_tiktok_perfil(texto: str) -> tuple[bool, str, str]:
    bruto = _limpar_url(texto)
    if not bruto:
        return False, bruto, (
            "⚠️ Para seguidores no TikTok, envie somente o @ ou link do perfil.\n\n"
            "Não envie link de vídeo/publicação."
        )

    if bruto.startswith("@") or (USERNAME_RE.fullmatch(bruto) and "." not in bruto):
        usuario = bruto if bruto.startswith("@") else f"@{bruto}"
        return True, usuario, ""

    ok, url, erro = validar_tiktok(bruto)
    if not ok:
        return ok, url, erro

    parsed = urlparse(_normalizar_url_social(url))
    host = _dominio(parsed.hostname or "")
    if host in {"vm.tiktok.com", "vt.tiktok.com"}:
        return False, url, (
            "⚠️ Para seguidores no TikTok, envie o @ ou o link completo do perfil.\n\n"
            "Links encurtados podem ser de vídeo e não são aceitos para seguidores."
        )

    if _tiktok_eh_video(url):
        return False, url, (
            "⚠️ Para seguidores no TikTok, envie somente o @ ou link do perfil.\n\n"
            "Link de vídeo/publicação não é aceito nesse serviço."
        )

    if not _tiktok_username_de_url(url):
        return False, url, (
            "⚠️ Para seguidores no TikTok, envie um @ ou link de perfil válido.\n\n"
            "Exemplo: @usuario ou https://www.tiktok.com/@usuario"
        )

    return True, _normalizar_url_social(url), ""


def validar_instagram_publicacao(texto: str) -> tuple[bool, str, str]:
    bruto = _limpar_url(texto)
    if not bruto:
        return False, bruto, (
            "⚠️ Para curtidas ou visualizações no Instagram, envie somente o link da publicação/Reel.\n\n"
            "Não envie @ nem link de perfil."
        )

    if bruto.startswith("@") or (USERNAME_RE.fullmatch(bruto) and "." not in bruto):
        return False, bruto, (
            "⚠️ Para curtidas ou visualizações no Instagram, não aceito @ de perfil.\n\n"
            "Envie o link direto da publicação/Reel. Exemplo: https://www.instagram.com/p/XXXX/"
        )

    ok, url, erro = validar_instagram(bruto)
    if not ok:
        return ok, url, erro
    if not _instagram_eh_publicacao(url):
        return False, url, (
            "⚠️ Para curtidas ou visualizações no Instagram, envie somente link de publicação/Reel.\n\n"
            "Link de perfil não é aceito nesse serviço."
        )
    return True, url, ""


def validar_tiktok_publicacao(texto: str) -> tuple[bool, str, str]:
    bruto = _limpar_url(texto)
    if not bruto:
        return False, bruto, (
            "⚠️ Para curtidas ou visualizações no TikTok, envie somente o link do vídeo/publicação.\n\n"
            "Não envie @ nem link de perfil."
        )

    if bruto.startswith("@") or (USERNAME_RE.fullmatch(bruto) and "." not in bruto):
        return False, bruto, (
            "⚠️ Para curtidas ou visualizações no TikTok, não aceito @ de perfil.\n\n"
            "Envie o link direto do vídeo/publicação. Exemplo: https://www.tiktok.com/@usuario/video/123456789"
        )

    ok, url, erro = validar_tiktok(bruto)
    if not ok:
        return ok, url, erro
    if not _tiktok_eh_video(url):
        return False, url, (
            "⚠️ Para curtidas ou visualizações no TikTok, envie somente link de vídeo/publicação.\n\n"
            "Link de perfil não é aceito nesse serviço."
        )
    return True, url, ""


def validar_destino_pedido(pedido: dict, texto: str) -> tuple[bool, str, str]:
    catalogo = str((pedido or {}).get("catalogo") or "").lower()
    tipo_destino = str((pedido or {}).get("tipo_destino") or "").strip().lower()
    if tipo_destino == "email" or "iptv" in catalogo or "internet" in catalogo or "assinatura" in catalogo:
        return validar_email(texto)

    # Verificação perfil/publicação removida: para Instagram e TikTok o bot aceita
    # @ ou link da plataforma sem bloquear por tipo de serviço antes do pagamento.
    if "instagram" in catalogo:
        return validar_instagram(texto)
    if "tiktok" in catalogo:
        return validar_tiktok(texto)

    destino = (texto or "").strip()
    if len(destino) < 3:
        return False, destino, "⚠️ Envie um link, @ ou identificação válida para o pedido."
    return True, destino, ""


# ---------------------------------------------------------------------------
# Verificação avançada antes de liberar pagamento para pedidos de redes sociais
# ---------------------------------------------------------------------------
# Objetivo:
# - Seguidores: aceitar somente @ ou link de perfil, bloquear publicação/vídeo/reel e confirmar que o perfil parece público.
# - Curtidas/visualizações: exigir link de publicação/vídeo/reel e confirmar que a página
#   parece acessível publicamente antes de gerar Pix.
#
# Observação importante: Instagram/TikTok podem mudar o HTML, bloquear datacenters ou
# pedir login. Por isso a checagem é feita por acesso público básico e pode ser ajustada
# por variáveis de ambiente.

_FALSE_VALUES = {"0", "false", "nao", "não", "no", "off", "desativado"}
_TRUE_VALUES = {"1", "true", "sim", "yes", "on", "ativado"}


def _env_bool(nome: str, padrao: bool) -> bool:
    valor = os.getenv(nome, "").strip().lower()
    if not valor:
        return padrao
    if valor in _FALSE_VALUES:
        return False
    if valor in _TRUE_VALUES:
        return True
    return padrao


def _env_int(nome: str, padrao: int) -> int:
    try:
        return int(os.getenv(nome, str(padrao)).strip())
    except (TypeError, ValueError):
        return padrao


# False = se não conseguir confirmar, bloqueia para não cobrar cliente com link inválido.
# True = se a rede social bloquear a checagem, libera somente com validação de formato.
CHECK_LINKS_FAIL_OPEN = _env_bool("CHECK_LINKS_FAIL_OPEN", False)
LINK_CHECK_TIMEOUT = _env_int("LINK_CHECK_TIMEOUT", 12)
LINK_CHECK_MAX_HTML = _env_int("LINK_CHECK_MAX_HTML", 350_000)

_LINK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}


class LinkSocialErro(Exception):
    pass


def _servico_chave_pedido(pedido: dict) -> str:
    texto = str((pedido or {}).get("servico_chave") or (pedido or {}).get("servico") or "").lower()
    texto = texto.replace("ç", "c").replace("õ", "o").replace("á", "a").replace("é", "e").replace("í", "i")
    return texto


def _catalogo_pedido(pedido: dict) -> str:
    return str((pedido or {}).get("catalogo") or "").strip().lower()


def _normalizar_url_social(url: str) -> str:
    url = _url_com_scheme(_limpar_url(url))
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc.lower()
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def _buscar_html_publico(url: str) -> tuple[str, int, str]:
    resposta = requests.get(
        url,
        headers=_LINK_HEADERS,
        timeout=LINK_CHECK_TIMEOUT,
        allow_redirects=True,
    )
    html = (resposta.text or "")[:LINK_CHECK_MAX_HTML]
    return str(resposta.url or url), int(resposta.status_code), html


def _mensagem_falha_rede(exc: Exception) -> str:
    if CHECK_LINKS_FAIL_OPEN:
        return ""
    return (
        "⚠️ Não consegui confirmar esse link agora.\n\n"
        "Para evitar cobrar por um pedido com link inválido, o pagamento não foi liberado. "
        "Envie outro link público ou tente novamente daqui a pouco."
    )


def _html_tem_algum(html: str, termos: list[str]) -> bool:
    baixo = (html or "").lower()
    return any(t.lower() in baixo for t in termos)


def _html_indica_privado(plataforma: str, html: str) -> bool:
    termos = [
        '"is_private":true',
        '"is_private" : true',
        '"privateaccount":true',
        '"privateaccount" : true',
        'private account',
        'this account is private',
        'conta privada',
        'esta conta é privada',
        'perfil privado',
    ]
    if plataforma == "instagram":
        termos.extend([
            'is private',
            'the link you followed may be broken',
        ])
    if plataforma == "tiktok":
        termos.extend([
            'user-private',
            'privateaccount',
        ])
    return _html_tem_algum(html, termos)


def _html_indica_inexistente(plataforma: str, html: str, status_code: int) -> bool:
    if status_code in {404, 410}:
        return True
    termos = [
        "page not found",
        "not found",
        "couldn't find this account",
        "couldn’t find this account",
        "this page isn't available",
        "this page is unavailable",
        "sorry, this page isn't available",
        "video currently unavailable",
        "vídeo indisponível",
        "publicação indisponível",
        "post unavailable",
    ]
    if plataforma == "instagram":
        termos.extend([
            "the link you followed may be broken",
            "a página que você solicitou não está disponível",
        ])
    if plataforma == "tiktok":
        termos.extend([
            "video unavailable",
            "account not found",
        ])
    return _html_tem_algum(html, termos)


def _instagram_username_de_url(url: str) -> str:
    parsed = urlparse(_normalizar_url_social(url))
    partes = [p for p in (parsed.path or "").split("/") if p]
    if not partes:
        return ""
    reservados = {"p", "reel", "reels", "tv", "stories", "explore", "accounts", "share"}
    if partes[0].lower() in reservados:
        return ""
    if USERNAME_RE.fullmatch(partes[0]):
        return partes[0]
    return ""


def _instagram_eh_publicacao(url: str) -> bool:
    parsed = urlparse(_normalizar_url_social(url))
    partes = [p.lower() for p in (parsed.path or "").split("/") if p]
    return len(partes) >= 2 and partes[0] in {"p", "reel", "reels", "tv"}


def _instagram_url_perfil(usuario_ou_url: str) -> str:
    texto = (usuario_ou_url or "").strip()
    if texto.startswith("@"):
        usuario = texto[1:]
    else:
        usuario = _instagram_username_de_url(texto) or texto.strip("/")
    usuario = usuario.strip("@/")
    return f"https://www.instagram.com/{usuario}/"


def _tiktok_username_de_url(url: str) -> str:
    parsed = urlparse(_normalizar_url_social(url))
    partes = [p for p in (parsed.path or "").split("/") if p]
    if partes and partes[0].startswith("@"):
        usuario = partes[0][1:]
        if USERNAME_RE.fullmatch(usuario):
            return usuario
    return ""


def _tiktok_eh_video(url: str) -> bool:
    parsed = urlparse(_normalizar_url_social(url))
    host = _dominio(parsed.hostname or "")
    partes = [p.lower() for p in (parsed.path or "").split("/") if p]
    if host in {"vm.tiktok.com", "vt.tiktok.com"}:
        # Shortlink: só dá para confirmar depois do redirect/http.
        return True
    return len(partes) >= 3 and partes[0].startswith("@") and partes[1] in {"video", "photo"}


def _tiktok_url_perfil(usuario_ou_url: str) -> str:
    texto = (usuario_ou_url or "").strip()
    if texto.startswith("@"):
        usuario = texto[1:]
    else:
        usuario = _tiktok_username_de_url(texto) or texto.strip("@/")
    usuario = usuario.strip("@/")
    return f"https://www.tiktok.com/@{usuario}"


def _validar_acesso_publico(plataforma: str, url: str, tipo: str) -> tuple[bool, str]:
    try:
        final_url, status_code, html = _buscar_html_publico(url)
    except Exception as exc:
        msg = _mensagem_falha_rede(exc)
        if not msg:
            return True, "Não foi possível confirmar acesso público; liberado por CHECK_LINKS_FAIL_OPEN."
        return False, msg

    if status_code in {401, 403, 429, 503}:
        if CHECK_LINKS_FAIL_OPEN:
            return True, f"Rede social retornou HTTP {status_code}; liberado por CHECK_LINKS_FAIL_OPEN."
        return False, (
            "⚠️ Não consegui confirmar se esse link está público.\n\n"
            f"A plataforma retornou HTTP {status_code}. Para evitar problema no pedido, envie um link público direto."
        )

    if _html_indica_inexistente(plataforma, html, status_code):
        return False, (
            "❌ Esse link parece estar indisponível ou não existe.\n\n"
            "Envie um link público válido para continuar."
        )

    if _html_indica_privado(plataforma, html):
        alvo = "perfil" if tipo == "perfil" else "publicação"
        return False, (
            f"🔒 Esse {alvo} parece estar privado ou inacessível.\n\n"
            "Deixe o perfil como público e envie o link novamente antes de pagar."
        )

    if tipo == "publicacao":
        # Depois de redirects, confere novamente o padrão de publicação/vídeo.
        if plataforma == "instagram" and not _instagram_eh_publicacao(final_url):
            return False, (
                "⚠️ Para curtidas ou visualizações, envie o link de uma publicação, Reel ou vídeo do Instagram.\n\n"
                "Não envie apenas o @ ou link do perfil."
            )
        if plataforma == "tiktok" and not _tiktok_eh_video(final_url):
            return False, (
                "⚠️ Para curtidas ou visualizações, envie o link de um vídeo/publicação do TikTok.\n\n"
                "Não envie apenas o @ ou link do perfil."
            )

    return True, "Link conferido: parece público e acessível."

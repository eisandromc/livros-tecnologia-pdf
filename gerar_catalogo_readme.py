from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import xml.etree.ElementTree as ET

try:
    import fitz  # type: ignore
except Exception:
    fitz = None


ARQUIVO_ATUAL = Path(__file__).name
ARQUIVO_SAIDA = Path("README.md")
ARQUIVO_INDICE = Path("conversao_indice.json")
EXTENSOES_SUPORTADAS = {".pdf", ".epub", ".txt", ".ppt", ".pptx"}
DIRETORIOS_IGNORADOS = {"__pycache__", ".git", "DUPLICADOS"}
MARCADOR_INICIO = "<!-- catalogo-livros:inicio -->"
MARCADOR_FIM = "<!-- catalogo-livros:fim -->"


@dataclass(slots=True)
class RegistroLivro:
    titulo: str
    editora: str
    ano: str
    autor: str
    caminho: Path
    link_titulo: Optional[Path] = None


def texto_limpo(valor: str | None) -> str:
    if not valor:
        return ""
    return re.sub(r"\s+", " ", str(valor)).strip()


def normalizar_campo(valor: str | None, padrao: str) -> str:
    texto = texto_limpo(valor)
    return texto if texto else padrao


def escapar_tabela(valor: str) -> str:
    return valor.replace("|", "\\|").replace("\n", " ").strip()


def nome_humano(stem: str) -> str:
    stem = re.sub(r"[_\-]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem.title() if stem else "Sem Titulo"


def extrair_ano_texto(valor: str | None) -> str:
    texto = texto_limpo(valor)
    correspondencia = re.search(r"(19|20)\d{2}", texto)
    return correspondencia.group(0) if correspondencia else "Nao informado"


def remover_acentos(texto: str) -> str:
    normalizado = unicodedata.normalize("NFD", texto or "")
    return "".join(caractere for caractere in normalizado if unicodedata.category(caractere) != "Mn")


def normalizar_nome_arquivo_processado(nome_arquivo: str) -> str:
    caminho = Path(nome_arquivo)
    stem = remover_acentos(caminho.stem)
    stem = re.sub(r"[^A-Za-z0-9\s\-|]+", " ", stem)
    stem = re.sub(r"\s*\|\s*", " | ", stem)
    stem = re.sub(r"\s*-\s*", "-", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" -|")

    partes = []
    for bloco in re.split(r"(\s+\|\s+|-|\s+)", stem):
        if not bloco:
            continue
        if re.fullmatch(r"\s+\|\s+|-|\s+", bloco):
            partes.append(bloco)
            continue
        partes.append(bloco[:1].upper() + bloco[1:].lower())

    nome_limpo = "".join(partes).strip() or "Documento"
    return f"{nome_limpo}{caminho.suffix.lower()}"


def extrair_do_nome(caminho: Path) -> dict[str, str]:
    stem = nome_humano(caminho.stem)
    partes = [parte.strip() for parte in re.split(r"\s+-\s+|\s+\|\s+|_", stem) if parte.strip()]

    titulo = partes[0] if partes else stem
    editora = "Nao informado"
    autor = "Nao informado"
    ano = extrair_ano_texto(stem)

    for parte in partes[1:]:
        if ano == "Nao informado":
            ano = extrair_ano_texto(parte)
        if autor == "Nao informado" and re.search(r"\bautor\b", parte, flags=re.IGNORECASE):
            autor = re.sub(r"(?i)\bautor\b[:\s-]*", "", parte).strip() or "Nao informado"
            continue
        if editora == "Nao informado" and re.search(r"\beditora\b", parte, flags=re.IGNORECASE):
            editora = re.sub(r"(?i)\beditora\b[:\s-]*", "", parte).strip() or "Nao informado"

    return {
        "titulo": normalizar_campo(titulo, "Sem titulo"),
        "editora": normalizar_campo(editora, "Nao informado"),
        "ano": normalizar_campo(ano, "Nao informado"),
        "autor": normalizar_campo(autor, "Nao informado"),
    }


def ler_mdls(caminho: Path) -> dict[str, str]:
    try:
        resultado = subprocess.run(
            [
                "mdls",
                "-name",
                "kMDItemTitle",
                "-name",
                "kMDItemAuthors",
                "-name",
                "kMDItemPublisher",
                "-name",
                "kMDItemContentCreationDate",
                str(caminho),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return {}

    dados: dict[str, str] = {}
    for linha in resultado.stdout.splitlines():
        if "=" not in linha:
            continue
        chave, valor = [parte.strip() for parte in linha.split("=", 1)]
        if valor == "(null)":
            continue
        if valor.startswith("(") and valor.endswith(")"):
            itens = [item.strip().strip('"') for item in valor[1:-1].split(",") if item.strip()]
            dados[chave] = ", ".join(itens)
        else:
            dados[chave] = valor.strip().strip('"')
    return dados


def extrair_pdf(caminho: Path) -> dict[str, str]:
    dados: dict[str, str] = {}
    if fitz is not None:
        try:
            with fitz.open(str(caminho)) as documento:
                metadata = documento.metadata or {}
            dados = {
                "titulo": texto_limpo(metadata.get("title")),
                "autor": texto_limpo(metadata.get("author")),
                "editora": texto_limpo(metadata.get("producer")) or texto_limpo(metadata.get("creator")),
                "ano": extrair_ano_texto(metadata.get("creationDate") or metadata.get("modDate")),
            }
        except Exception:
            dados = {}

    if not any(dados.values()):
        mdls = ler_mdls(caminho)
        dados = {
            "titulo": texto_limpo(mdls.get("kMDItemTitle")),
            "autor": texto_limpo(mdls.get("kMDItemAuthors")),
            "editora": texto_limpo(mdls.get("kMDItemPublisher")),
            "ano": extrair_ano_texto(mdls.get("kMDItemContentCreationDate")),
        }
    return dados


def extrair_epub(caminho: Path) -> dict[str, str]:
    try:
        with zipfile.ZipFile(caminho) as pacote:
            container = ET.fromstring(pacote.read("META-INF/container.xml"))
            namespace_container = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
            rootfile = container.find(".//c:rootfile", namespace_container)
            if rootfile is None:
                return {}
            opf_path = rootfile.attrib.get("full-path")
            if not opf_path:
                return {}
            opf = ET.fromstring(pacote.read(opf_path))
    except Exception:
        return {}

    namespace = {
        "dc": "http://purl.org/dc/elements/1.1/",
        "opf": "http://www.idpf.org/2007/opf",
    }
    return {
        "titulo": opf.findtext(".//dc:title", default="", namespaces=namespace),
        "autor": opf.findtext(".//dc:creator", default="", namespaces=namespace),
        "editora": opf.findtext(".//dc:publisher", default="", namespaces=namespace),
        "ano": extrair_ano_texto(opf.findtext(".//dc:date", default="", namespaces=namespace)),
    }


def extrair_pptx(caminho: Path) -> dict[str, str]:
    try:
        with zipfile.ZipFile(caminho) as pacote:
            core = ET.fromstring(pacote.read("docProps/core.xml"))
    except Exception:
        return {}

    namespace = {
        "dc": "http://purl.org/dc/elements/1.1/",
        "dcterms": "http://purl.org/dc/terms/",
    }
    mdls = ler_mdls(caminho)
    return {
        "titulo": core.findtext(".//dc:title", default="", namespaces=namespace),
        "autor": core.findtext(".//dc:creator", default="", namespaces=namespace),
        "editora": texto_limpo(mdls.get("kMDItemPublisher")),
        "ano": extrair_ano_texto(
            core.findtext(".//dcterms:created", default="", namespaces=namespace)
            or core.findtext(".//dcterms:modified", default="", namespaces=namespace)
        ),
    }


def extrair_txt(caminho: Path) -> dict[str, str]:
    try:
        with open(caminho, "r", encoding="utf-8", errors="ignore") as arquivo:
            linhas = [arquivo.readline().strip() for _ in range(40)]
    except Exception:
        return {}

    dados: dict[str, str] = {}
    mapa = {
        "titulo": r"(?i)^t[íi]tulo\s*:\s*(.+)$",
        "autor": r"(?i)^autor\s*:\s*(.+)$",
        "editora": r"(?i)^editora\s*:\s*(.+)$",
        "ano": r"(?i)^ano(?:\s+de\s+lan[cç]amento)?\s*:\s*(.+)$",
    }
    for linha in linhas:
        for campo, padrao in mapa.items():
            correspondencia = re.match(padrao, linha)
            if correspondencia and campo not in dados:
                dados[campo] = correspondencia.group(1).strip()
    if "ano" in dados:
        dados["ano"] = extrair_ano_texto(dados["ano"])
    return dados


def extrair_ppt(caminho: Path) -> dict[str, str]:
    mdls = ler_mdls(caminho)
    return {
        "titulo": texto_limpo(mdls.get("kMDItemTitle")),
        "autor": texto_limpo(mdls.get("kMDItemAuthors")),
        "editora": texto_limpo(mdls.get("kMDItemPublisher")),
        "ano": extrair_ano_texto(mdls.get("kMDItemContentCreationDate")),
    }


def extrair_frontmatter_markdown(texto: str) -> tuple[dict[str, str], str]:
    if not texto.startswith("---"):
        return {}, texto

    linhas = texto.splitlines()
    fim = None
    for indice in range(1, len(linhas)):
        if linhas[indice].strip() == "---":
            fim = indice
            break

    if fim is None:
        return {}, texto

    dados: dict[str, str] = {}
    for linha in linhas[1:fim]:
        if ":" not in linha:
            continue
        chave, valor = linha.split(":", 1)
        chave = chave.strip().lower()
        valor = valor.strip().strip('"').strip("'")
        if valor and not valor.startswith("-"):
            dados[chave] = valor

    return dados, "\n".join(linhas[fim + 1 :])


def extrair_autor_markdown(corpo: str) -> str:
    for linha in corpo.splitlines():
        texto = linha.strip()
        if not texto or texto.startswith("!["):
            continue
        if texto.startswith("# "):
            candidato = texto[2:].strip()
            if len(candidato) >= 3:
                return candidato
    return "Nao informado"


def extrair_markdown(caminho: Path) -> dict[str, str]:
    try:
        texto = caminho.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return {}

    frontmatter, corpo = extrair_frontmatter_markdown(texto)
    return {
        "titulo": texto_limpo(frontmatter.get("title")),
        "editora": texto_limpo(frontmatter.get("publisher")),
        "ano": extrair_ano_texto(frontmatter.get("year")),
        "autor": texto_limpo(frontmatter.get("author"))
        or texto_limpo(frontmatter.get("authors"))
        or extrair_autor_markdown(corpo),
    }


def carregar_mapeamento_markdown_para_original() -> tuple[dict[str, str], dict[str, str]]:
    if not ARQUIVO_INDICE.exists():
        return {}, {}

    try:
        indice = json.loads(ARQUIVO_INDICE.read_text(encoding="utf-8"))
    except Exception:
        return {}, {}

    por_markdown: dict[str, str] = {}
    por_nome_base: dict[str, str] = {}
    for chave in ("hashes", "livros", "assinaturas"):
        bucket = indice.get(chave, {})
        if not isinstance(bucket, dict):
            continue
        for registro in bucket.values():
            if not isinstance(registro, dict):
                continue
            markdown = registro.get("markdown")
            arquivo_original = registro.get("arquivo_original")
            nome_base = registro.get("nome_base")
            if markdown and arquivo_original:
                por_markdown[str(Path(markdown).as_posix())] = str(arquivo_original)
            if nome_base and arquivo_original:
                por_nome_base[str(nome_base)] = str(arquivo_original)
    return por_markdown, por_nome_base


def construir_mapa_arquivos_disponiveis() -> dict[str, Path]:
    mapa: dict[str, Path] = {}
    diretorios = [Path("."), Path("ARQUIVOS_ORIGINAIS_PROCESSADOS"), Path("DUPLICADOS")]
    for diretorio in diretorios:
        if not diretorio.exists() or not diretorio.is_dir():
            continue
        for caminho in diretorio.iterdir():
            if not caminho.is_file():
                continue
            mapa.setdefault(caminho.name.lower(), caminho)
    return mapa


def resolver_link_titulo(
    caminho: Path,
    por_markdown: dict[str, str],
    por_nome_base: dict[str, str],
    arquivos_disponiveis: dict[str, Path],
) -> Optional[Path]:
    if caminho.suffix.lower() == ".pdf":
        return caminho

    if caminho.suffix.lower() != ".md":
        return caminho if caminho.exists() else None

    relativo = caminho.as_posix()
    nome_original = por_markdown.get(relativo) or por_nome_base.get(caminho.stem)
    if not nome_original:
        return None

    candidatos = [nome_original]
    candidatos.append(normalizar_nome_arquivo_processado(nome_original))

    for candidato in candidatos:
        encontrado = arquivos_disponiveis.get(candidato.lower())
        if encontrado and encontrado.suffix.lower() == ".pdf":
            return encontrado
    return None


def extrair_metadados_arquivo(
    caminho: Path,
    por_markdown: dict[str, str],
    por_nome_base: dict[str, str],
    arquivos_disponiveis: dict[str, Path],
) -> RegistroLivro:
    base = extrair_do_nome(caminho)
    extratores = {
        ".pdf": extrair_pdf,
        ".epub": extrair_epub,
        ".txt": extrair_txt,
        ".ppt": extrair_ppt,
        ".pptx": extrair_pptx,
        ".md": extrair_markdown,
    }
    extras = extratores.get(caminho.suffix.lower(), lambda _: {})(caminho)

    return RegistroLivro(
        titulo=normalizar_campo(extras.get("titulo") or base["titulo"], "Sem titulo"),
        editora=normalizar_campo(extras.get("editora") or base["editora"], "Nao informado"),
        ano=normalizar_campo(extras.get("ano") or base["ano"], "Nao informado"),
        autor=normalizar_campo(extras.get("autor") or base["autor"], "Nao informado"),
        caminho=caminho,
        link_titulo=resolver_link_titulo(caminho, por_markdown, por_nome_base, arquivos_disponiveis),
    )


def caminho_ignorado(caminho: Path) -> bool:
    return any(parte in DIRETORIOS_IGNORADOS for parte in caminho.parts)


def localizar_markdowns() -> list[Path]:
    arquivos = []
    for caminho in Path(".").rglob("*.md"):
        if caminho.name == ARQUIVO_SAIDA.name:
            continue
        if len(caminho.parts) < 2:
            continue
        if caminho_ignorado(caminho):
            continue
        arquivos.append(caminho)
    return sorted(arquivos, key=lambda item: item.as_posix().lower())


def localizar_outros_arquivos() -> list[Path]:
    arquivos = []
    for caminho in Path(".").iterdir():
        if not caminho.is_file():
            continue
        if caminho.name in {ARQUIVO_SAIDA.name, ARQUIVO_ATUAL, ARQUIVO_INDICE.name}:
            continue
        if caminho.suffix.lower() not in EXTENSOES_SUPORTADAS:
            continue
        arquivos.append(caminho)
    return sorted(arquivos, key=lambda item: item.name.lower())


def formatar_titulo(registro: RegistroLivro) -> str:
    titulo = escapar_tabela(registro.titulo)
    if registro.link_titulo is None:
        return titulo
    return f"[{titulo}]({registro.link_titulo.as_posix()})"


def gerar_tabela(registros: list[RegistroLivro]) -> str:
    if not registros:
        return "_Nenhum arquivo encontrado._"

    linhas = [
        "| Titulo | Editora | Ano Lancamento | Autor | Arquivo |",
        "| --- | --- | --- | --- | --- |",
    ]
    for registro in registros:
        link_arquivo = f"[{escapar_tabela(registro.caminho.name)}]({registro.caminho.as_posix()})"
        linhas.append(
            "| "
            + " | ".join(
                [
                    formatar_titulo(registro),
                    escapar_tabela(registro.editora),
                    escapar_tabela(registro.ano),
                    escapar_tabela(registro.autor),
                    link_arquivo,
                ]
            )
            + " |"
        )
    return "\n".join(linhas)


def montar_bloco_catalogo(registros: list[RegistroLivro], origem: str) -> str:
    instante = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tabela = gerar_tabela(registros)
    return (
        f"{MARCADOR_INICIO}\n"
        "## Catalogo de Arquivos\n\n"
        f"Origem: `{origem}`\n\n"
        f"Atualizado em: `{instante}`\n\n"
        f"{tabela}\n"
        f"{MARCADOR_FIM}"
    )


def atualizar_readme(bloco_catalogo: str) -> None:
    if ARQUIVO_SAIDA.exists():
        conteudo_atual = ARQUIVO_SAIDA.read_text(encoding="utf-8", errors="ignore")
    else:
        conteudo_atual = "# Catalogo de Livros\n"

    padrao = re.compile(
        rf"{re.escape(MARCADOR_INICIO)}.*?{re.escape(MARCADOR_FIM)}",
        flags=re.DOTALL,
    )
    if padrao.search(conteudo_atual):
        novo_conteudo = padrao.sub(bloco_catalogo, conteudo_atual)
    else:
        novo_conteudo = f"{conteudo_atual.rstrip()}\n\n{bloco_catalogo}\n"

    novo_conteudo = novo_conteudo.replace(
        "# Catalogo de Livros<!--",
        "# Catalogo de Livros\n\n<!--",
    )
    ARQUIVO_SAIDA.write_text(novo_conteudo, encoding="utf-8")


def criar_registros(arquivos: list[Path]) -> list[RegistroLivro]:
    por_markdown, por_nome_base = carregar_mapeamento_markdown_para_original()
    arquivos_disponiveis = construir_mapa_arquivos_disponiveis()
    registros = [
        extrair_metadados_arquivo(caminho, por_markdown, por_nome_base, arquivos_disponiveis)
        for caminho in arquivos
    ]
    registros.sort(key=lambda item: item.titulo.lower())
    return registros


def criar_readme_de_markdowns() -> int:
    registros = criar_registros(localizar_markdowns())
    atualizar_readme(montar_bloco_catalogo(registros, "Arquivos MD"))
    print(f"[*] README.md criado a partir de {len(registros)} arquivo(s) Markdown.")
    return len(registros)


def criar_readme_de_outros_arquivos() -> int:
    registros = criar_registros(localizar_outros_arquivos())
    atualizar_readme(montar_bloco_catalogo(registros, "Outros arquivos"))
    print(f"[*] README.md criado a partir de {len(registros)} outro(s) arquivo(s).")
    return len(registros)


def aguardar_voltar_menu() -> None:
    while True:
        print("\n[ 5 ] Voltar para o menu")
        opcao = input("> ").strip()
        if opcao == "5":
            return
        print("[!] Opcao invalida. Digite 5 para voltar ao menu principal.")


def exibir_menu() -> None:
    print("\n[ 1 ] Criar de arquivos MD")
    print("[ 2 ] Criar de outros arquivos")
    print("[ 3 ] Sair")


def main() -> None:
    while True:
        exibir_menu()
        opcao = input("> ").strip()

        if opcao == "1":
            criar_readme_de_markdowns()
            aguardar_voltar_menu()
            continue

        if opcao == "2":
            criar_readme_de_outros_arquivos()
            aguardar_voltar_menu()
            continue

        if opcao == "3":
            print("[*] Encerrando.")
            return

        print("[!] Opcao invalida. Escolha 1, 2 ou 3.")


if __name__ == "__main__":
    main()

# Controle de Estoque

Serviço de controle de estoque com **consistência forte sob alta concorrência**: entrada, saída, reserva, bloqueio, liberação e consulta, com a garantia de que nunca é possível vender, reservar ou bloquear uma unidade que não existe — mesmo com centenas de requisições simultâneas sobre o mesmo produto.

Python 3.11+ · FastAPI · PostgreSQL (lock pessimista de linha) · pytest · Docker

---

## Índice

1. [Execução rápida](#1-execução-rápida)
2. [O modelo de estoque em uma figura](#2-o-modelo-de-estoque-em-uma-figura)
3. [Arquitetura](#3-arquitetura)
4. [Concorrência: o problema e a solução](#4-concorrência-o-problema-e-a-solução)
5. [Tratamento de erros e resiliência](#5-tratamento-de-erros-e-resiliência)
6. [API HTTP](#6-api-http)
7. [Testes](#7-testes)
8. [Docker](#8-docker)
9. [Decisões técnicas e trade-offs](#9-decisões-técnicas-e-trade-offs)
10. [Premissas assumidas](#10-premissas-assumidas)
11. [Premissas de volumetria e capacidade](#11-premissas-de-volumetria-e-capacidade)
12. [Limitações conhecidas](#12-limitações-conhecidas)
13. [Os 5 aspectos mais importantes para produção](#13-os-5-aspectos-mais-importantes-para-produção)
14. [O que eu faria diferente em produção real](#14-o-que-eu-faria-diferente-em-produção-real)
15. [Uso de IA](#15-uso-de-ia)

---

## 1. Execução rápida

### Com Docker (recomendado — nada além de Docker é necessário)

```bash
docker compose up --build            # sobe PostgreSQL + API em http://localhost:8000
```

Documentação interativa da API: <http://localhost:8000/docs> · Health check: <http://localhost:8000/health>

```bash
# Suíte de testes completa (unitários + contrato + API + integração com PostgreSQL)
docker compose --profile test run --rm --build tests

# Derrubar tudo, inclusive o volume do banco
docker compose down -v
```

### Teste de fumaça (copie e cole)

```bash
curl -X POST localhost:8000/products/sku-1/entries      -H 'content-type: application/json' -d '{"quantity": 100}'
curl -X POST localhost:8000/products/sku-1/reservations -H 'content-type: application/json' -d '{"quantity": 30, "request_id": "pedido-1"}'
curl -X POST localhost:8000/products/sku-1/blocks       -H 'content-type: application/json' -d '{"quantity": 20, "reason": "avaria"}'
curl localhost:8000/products/sku-1
# {"product_id":"sku-1","total":100,"available":50,"reserved":30,"blocked":20}

curl -X POST localhost:8000/products/sku-1/withdrawals -H 'content-type: application/json' -d '{"quantity": 51}'
# HTTP 409 {"error":{"code":"insufficient_stock","details":{"requested":51,"available":50}}}
```

### Sem Docker (local)

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

# Modo mais simples: backend em memória, sem banco nenhum
uvicorn stock.api.app:create_app --factory --app-dir src --reload

# Com PostgreSQL:
export DATABASE_URL=postgresql://user:senha@localhost:5432/stock
uvicorn stock.api.app:create_app --factory --app-dir src

pytest                                  # testes de integração são pulados sem DATABASE_URL
DATABASE_URL=postgresql://... pytest    # suíte completa
```

### Variáveis de ambiente

| Variável | Padrão | Função |
|---|---|---|
| `STOCK_BACKEND` | `postgres` se houver `DATABASE_URL`, senão `memory` | Escolhe a persistência |
| `DATABASE_URL` | — | DSN do PostgreSQL |
| `DB_POOL_MIN` / `DB_POOL_MAX` | `2` / `10` | Tamanho do pool de conexões **por processo** |
| `DB_POOL_TIMEOUT_S` | `5.0` | Espera máxima por uma conexão livre (backpressure) |
| `DB_LOCK_TIMEOUT_MS` | `2000` | Espera máxima pelo lock da linha do produto |
| `DB_STATEMENT_TIMEOUT_MS` | `5000` | Teto por instrução SQL |
| `MEMORY_LOCK_TIMEOUT_S` | `5.0` | Equivalente no backend em memória |
| `WEB_CONCURRENCY` | `1` | Processos uvicorn |
| `LOG_LEVEL` | `INFO` | Nível de log |

---

## 2. O modelo de estoque em uma figura

```
                    total = 100
   ┌─────────────────────────────────────────────────┐
   │ disponível 50  │ reservado 30   │ bloqueado 20  │
   └─────────────────────────────────────────────────┘
          ▲                 │                │
          │   reservar ─────┘                │
          │   bloquear ──────────────────────┘
          └──── liberar ─────────────────────┘

   entrada  →  aumenta total (e disponível)
   saída    →  diminui total, consumindo APENAS disponível
```

**A decisão de modelagem mais importante:** `total` é a fonte da verdade e **`disponível` é derivado**, nunca armazenado:

```
disponível = total − reservado − bloqueado
```

Guardar quatro contadores independentes significaria manter quatro números sincronizados em toda operação — e qualquer caminho de código que esquecesse um deles produziria um estoque que não fecha. Com um valor derivado, a inconsistência é **aritmeticamente impossível**, não apenas improvável. A invariante do sistema inteiro cabe em uma linha:

```
reservado + bloqueado ≤ total      (equivale a: disponível ≥ 0)
```

Reservas e bloqueios são a **mesma estrutura** (`Hold`) com um discriminador `kind`, porque se comportam igual: consomem disponível, têm identidade própria, podem ser consultados e liberados. A diferença é semântica — a reserva aponta para uma solicitação (`request_id`, obrigatório), o bloqueio para um motivo (`reason`, opcional). Modelar como duas classes gêmeas duplicaria o ciclo de vida inteiro; o `kind` é verificado na liberação, então um id de reserva não libera um bloqueio e vice-versa.

---

## 3. Arquitetura

Arquitetura em camadas com **inversão de dependência**: o domínio não conhece banco, HTTP nem framework.

```
┌──────────────────────────────────────────────────────────┐
│  api/app.py          FastAPI: HTTP ⇄ casos de uso        │  traduz, não decide
├──────────────────────────────────────────────────────────┤
│  application/        StockService  (casos de uso)        │  orquestra a transação
│                      ports.py      (interfaces)          │  e o lock
├──────────────────────────────────────────────────────────┤
│  domain/             Product (agregado), Hold            │  REGRAS DE NEGÓCIO
│                      exceptions.py                       │  puro, sem I/O
├──────────────────────────────────────────────────────────┤
│  infrastructure/     PostgresBackend · InMemoryBackend   │  implementa as portas
└──────────────────────────────────────────────────────────┘
          as setas de dependência apontam para dentro ↑
```

```
src/stock/
├── domain/
│   ├── models.py        Product (agregado), Hold, validações  — sem I/O, sem relógio
│   └── exceptions.py    hierarquia de erros com code + details
├── application/
│   ├── ports.py         ProductRepository, HoldRepository, UnitOfWork (Protocol)
│   └── service.py       casos de uso: o molde da seção crítica
├── infrastructure/
│   ├── postgres.py      SELECT ... FOR UPDATE, pool, constraints, tradução de erros
│   └── memory.py        threading.Lock por produto (dev e testes)
├── api/app.py           FastAPI, schemas, mapeamento erro → HTTP
└── config.py            configuração por variáveis de ambiente
```

**Por que esta separação, e não tudo num arquivo?** Porque é ela que permite rodar **a mesma suíte de testes de concorrência contra dois backends diferentes**. Se a regra de negócio estivesse dentro do SQL ou do handler HTTP, não haveria como provar que o comportamento é idêntico em memória e no PostgreSQL — e não haveria como trocar a persistência sem reescrever as regras.

### Papéis de cada camada

**Domínio (`Product`)** — encapsulamento forte. Os contadores são privados (`_total`, `_reserved`, `_blocked`, com `__slots__`) e expostos só por propriedades de leitura. Não existe setter: o estado só muda por `add_stock`, `remove_stock`, `reserve`, `block`, `release`. Cada método **valida antes de mutar**, então uma operação rejeitada deixa o objeto exatamente como estava — não existe mutação parcial. Depois de cada mutação, `_check_invariants()` confere a invariante; `Product.restore()` faz o mesmo ao reidratar do banco, recusando dado já corrompido em vez de propagá-lo.

O domínio não sabe que horas são nem gera ids: relógio e gerador de ids são **injetados** no `StockService`. Isso mantém o domínio determinístico e testável — e, como se vê na seção de testes, permite injetar um relógio lento para alargar a janela de corrida e provar que o lock funciona.

**Aplicação (`StockService`)** — dona da transação e do lock. Todo comando segue o mesmo molde:

```python
def reserve(self, product_id, quantity, request_id):
    pid = validate_identifier(product_id)      # 1. fail-fast FORA do lock
    qty = validate_quantity(quantity)          #    (lock curto = mais vazão)
    with self._uow_factory() as uow:           # 2. abre a unidade de trabalho
        product = self._locked_product(uow, pid)   # 3. LOCK PESSIMISTA
        hold = product.reserve(qty, request_id, self._new_id(), self._now())  # 4. regra
        uow.products.save(product)             # 5. persiste produto + hold
        uow.holds.save(hold)                   #    na MESMA transação
        uow.commit()                           # 6. commit → libera o lock
    return hold                                #    (saída sem commit = rollback)
```

**Infraestrutura** — implementa `UnitOfWork`. Dois backends, um contrato:

| | `InMemoryBackend` | `PostgresBackend` |
|---|---|---|
| Exclusão mútua | `threading.Lock` por produto | `SELECT … FOR UPDATE` |
| Atomicidade | buffer aplicado no commit | transação do banco |
| Escopo seguro | **um processo** | **várias réplicas** |
| Uso | dev local, testes rápidos | produção |

---

## 4. Concorrência: o problema e a solução

### O problema concreto

Toda operação de estoque é um **check-then-act**: ler o estado, decidir se cabe, gravar. Entre o "ler" e o "gravar" existe uma janela. Com duas requisições simultâneas e 1 unidade em estoque:

```
tempo   Requisição A                    Requisição B
  │     lê: disponível = 1
  │                                     lê: disponível = 1
  │     1 ≤ 1 ✓ pode reservar
  │                                     1 ≤ 1 ✓ pode reservar
  │     grava reservado = 1
  ▼                                     grava reservado = 1
        ───────────────────────────────────────────────────
        Resultado: 2 unidades reservadas, 1 existe. Oversell.
```

Nenhuma das duas requisições fez nada errado isoladamente. Validação, tipagem e testes de unidade não detectam isso — o bug só existe no entrelaçamento. **Este não é um risco teórico: medi.** Removendo o lock dos dois backends e rodando a suíte, 300 reservas concorrentes de 1 unidade sobre um estoque de 100 resultaram em **300 aceitas** (seção [Testes](#7-testes)).

### A solução escolhida: lock pessimista por produto

```sql
SELECT total, reserved, blocked FROM products WHERE id = %s FOR UPDATE;
```

`FOR UPDATE` trava **a linha daquele produto** até o fim da transação. Transações que disputam o mesmo produto são serializadas pelo banco; produtos diferentes seguem **totalmente em paralelo**. A janela da corrida deixa de existir porque o "ler → decidir → gravar" inteiro acontece dentro da seção crítica.

```
Requisição A ──[ lock sku-1 ]── lê · decide · grava ──[ commit, unlock ]──▶
Requisição B ────── espera o lock ─────────▶ lê (já atualizado) · recusa ──▶
Requisição C ──[ lock sku-2 ]── lê · decide · grava ──▶  (outro produto: paralelo)
```

O **granularidade do lock é o produto**, não a tabela nem o sistema. A vazão escala com o número de produtos distintos: 10.000 SKUs movimentando em paralelo não se atrapalham. O gargalo é a contenção em um único SKU muito quente — e esse é um gargalo intrínseco ao requisito de não vender o que não existe, não um defeito da implementação.

### Por que lock pessimista, e não as alternativas

| Estratégia | Por que não foi escolhida aqui |
|---|---|
| **Lock otimista** (coluna `version`, `UPDATE … WHERE version = N`) | Correto e sem bloqueio, mas sob alta contenção no mesmo SKU degrada: a transação só descobre o conflito **no fim** e precisa refazer todo o trabalho. Com N requisições concorrentes, até N−1 retries desperdiçados. Pessimista paga uma espera curta e faz o trabalho **uma vez**. É a escolha certa quando a contenção é esperada — exatamente o cenário de alta concorrência proposto. |
| **Two-Phase Commit (2PC)** | 2PC coordena uma transação **entre vários gerenciadores de recursos** (ex.: banco + broker de mensagens). Aqui existe **um único banco**: toda a unidade de trabalho (contadores do produto + registro do hold) cabe em **uma transação ACID local**, que já é atômica. Adicionar 2PC traria coordenador, logs de preparação, locks mantidos durante toda a fase de prepare e o risco clássico de bloqueio se o coordenador cair — custo e modos de falha novos **sem ganho de correção**. 2PC passaria a ser pauta se o estoque precisasse confirmar junto com outro sistema transacional; nesse caso, a recomendação ainda seria **Saga com compensação** (a reserva já é, por natureza, um passo compensável: liberar) em vez de 2PC. |
| **`UPDATE` atômico com `WHERE` condicional** | `UPDATE products SET reserved = reserved + 5 WHERE id = ? AND total - reserved - blocked >= 5` é atômico e muito rápido. Mas joga a regra de negócio para dentro do SQL: fica invisível para testes de domínio, impossível de reaproveitar entre operações e cada regra nova vira mais SQL condicional. Ao retornar 0 linhas, perde-se a informação de *por quê* (não havia estoque? o produto não existe?), degradando a mensagem de erro. Trade-off consciente: **alguns microssegundos a mais por um domínio expressivo e testável**. |
| **Serializable (SSI)** | Correto, mas transfere o problema para o tratamento de erros de serialização e retries em toda a aplicação, com custo maior sob contenção alta. |
| **Lock distribuído externo** (Redis/etcd) | Introduz um segundo sistema no caminho crítico, com lease, fencing token e split-brain para gerenciar. O PostgreSQL já oferece locks corretos e duráveis, **na mesma transação que grava os dados** — o que elimina a janela entre "soltei o lock" e "gravei". |

### Prevenção de deadlock por design

**Cada operação trava exatamente um produto.** Como nunca há dois locks simultâneos, não existe espera circular — a condição necessária para deadlock simplesmente não ocorre. Não é disciplina de código que precisa ser lembrada em cada revisão: é propriedade estrutural do desenho. Se um requisito futuro exigir operações multi-produto (transferência entre depósitos, por exemplo), a regra passa a ser obrigatória: **adquirir os locks sempre em ordem determinística** (ordenados por `product_id`) — e isso deve vir acompanhado de teste.

### Fail-fast sob contenção

Esperar indefinidamente por um lock é pior do que falhar: as requisições empilham, o pool de conexões esgota e o travamento de um SKU quente derruba o serviço inteiro. Por isso **toda transação define seus próprios limites**:

```python
SELECT set_config('lock_timeout', '2000ms', true),      -- espera máx. pelo lock
       set_config('statement_timeout', '5000ms', true)  -- teto por instrução
```

Estourado o prazo, o cliente recebe **503 com `Retry-After`** — um sinal honesto de "tente de novo", e não um timeout silencioso. O pool também tem teto e `timeout` de checkout: quando satura, a aplicação **rejeita rápido** em vez de acumular trabalho que não dá conta (backpressure explícito). Há teste cobrindo exatamente isto, inclusive provando que um produto travado **não bloqueia os demais**.

### Defesa em profundidade

A regra vive no domínio, mas o banco é a última linha:

```sql
CONSTRAINT products_non_negative            CHECK (total >= 0 AND reserved >= 0 AND blocked >= 0),
CONSTRAINT products_available_non_negative  CHECK (reserved + blocked <= total)
```

Se um bug futuro, um script manual ou um caminho de código não previsto tentar gravar estoque negativo, o banco **recusa a transação inteira**. A violação é traduzida para `InvariantViolationError` → HTTP 500 com log detalhado: trata-se de um defeito nosso, não de um erro do cliente. Há teste que burla o código, escreve SQL direto e confirma que as três constraints seguram.

---

## 5. Tratamento de erros e resiliência

Exceções customizadas organizadas por **o que o chamador deve fazer**, não por onde o erro ocorreu:

```
StockError
├── BusinessRuleViolation      o pedido viola uma regra — repetir igual não resolve
│   ├── InvalidQuantityError          422   quantidade não inteira, ≤ 0 ou acima do teto
│   ├── InvalidIdentifierError        422   id/request_id fora do formato
│   ├── InsufficientStockError        409   não há disponível suficiente
│   └── HoldAlreadyReleasedError      409   reserva/bloqueio já liberado
├── NotFoundError
│   ├── ProductNotFoundError          404
│   └── HoldNotFoundError             404
├── InvariantViolationError           500   bug ou dado corrompido — nunca culpa do cliente
└── TransientError (retryable)        503 + Retry-After
    ├── LockTimeoutError                    contenção no produto
    ├── ConcurrencyConflictError            deadlock detectado pelo banco
    └── StorageUnavailableError             banco fora do ar / pool esgotado
```

Toda exceção carrega `code` (string estável, para clientes programáticos) e `details` (contexto estruturado). A API devolve sempre o mesmo envelope:

```json
{"error": {"code": "insufficient_stock",
           "message": "Não é possível reservar 51 unidade(s): apenas 50 disponível(is).",
           "details": {"operation": "reservar", "requested": 51, "available": 50}}}
```

O cliente recebe **quanto havia disponível**, não apenas "deu erro" — o que permite decidir entre reservar parcialmente, aguardar ou cancelar sem uma segunda chamada.

**Distinção que importa:** `409 insufficient_stock` é um resultado de negócio legítimo (o estoque acabou), não uma falha do sistema. `503` é uma falha operacional transitória e **é seguro repetir**. `500` indica defeito nosso. Essa separação é o que permite ao cliente implementar retry automático apenas onde ele faz sentido, e à operação alertar apenas no que é realmente problema.

**Nenhuma operação deixa o estoque inconsistente.** Três mecanismos em camadas:

1. **Domínio:** valida antes de mutar — operação inválida nem chega a alterar o objeto.
2. **Unit of Work:** sair do bloco `with` sem `commit()` faz rollback e libera os locks, **inclusive quando uma exceção sobe** no meio da operação.
3. **Banco:** constraints `CHECK` recusam qualquer estado impossível.

Erros do driver são traduzidos para o vocabulário do domínio na fronteira da infraestrutura (`psycopg.errors.LockNotAvailable` → `LockTimeoutError`, e assim por diante): nenhum detalhe de PostgreSQL vaza para a camada de aplicação ou para o cliente HTTP. A `InvariantViolationError` é o único caso em que a mensagem interna **não** é devolvida ao cliente — vai só para o log, para não expor detalhes internos (há teste verificando isso).

---

## 6. API HTTP

| Método | Rota | Ação | Sucesso | Erros possíveis |
|---|---|---|---|---|
| `POST` | `/products/{id}/entries` | Entrada (cria o produto na 1ª vez) | 200 | 422 |
| `POST` | `/products/{id}/withdrawals` | Saída | 200 | 404, 409, 422, 503 |
| `POST` | `/products/{id}/reservations` | Reservar | 201 | 404, 409, 422, 503 |
| `POST` | `/products/{id}/blocks` | Bloquear | 201 | 404, 409, 422, 503 |
| `POST` | `/reservations/{id}/release` | Liberar reserva | 200 | 404, 409, 503 |
| `POST` | `/blocks/{id}/release` | Liberar bloqueio | 200 | 404, 409, 503 |
| `GET` | `/products/{id}` | Consultar estoque | 200 | 404 |
| `GET` | `/reservations/{id}` · `/blocks/{id}` | Consultar hold | 200 | 404 |
| `GET` | `/health` | Liveness | 200 | — |

Reservas e bloqueios têm **rotas de liberação distintas** em vez de um `/holds/{id}/release` genérico: o tipo é verificado, de modo que um id de reserva não pode liberar um bloqueio por engano. Holds liberados **não são apagados** — mudam para `status: released` com `released_at`. Isso preserva auditoria e distingue "nunca existiu" (404) de "já foi liberado" (409).

Os endpoints são funções **síncronas** (`def`, não `async def`): o FastAPI as executa num threadpool limitado, o que combina com o driver síncrono do PostgreSQL e naturalmente limita o número de operações em voo.

---

## 7. Testes

**155 testes.** Executados contra PostgreSQL 16 real nesta entrega:

```
155 passed in 16.68s                        # com DATABASE_URL (memória + PostgreSQL)
124 passed, 31 skipped in 4.26s             # sem DATABASE_URL (integração pulada, não falha)
```

| Suíte | O que cobre |
|---|---|
| `tests/unit/` | Agregado `Product` puro: caminho feliz, limites, entradas inválidas, invariantes |
| `tests/contract/test_service.py` | Casos de uso **contra os dois backends** |
| `tests/contract/test_concurrency.py` | Corridas reais com 32 threads **contra os dois backends** |
| `tests/api/` | Status HTTP, envelope de erro, falhas transitórias |
| `tests/integration/` | Constraints do PostgreSQL, tradução de erros, reconciliação em SQL |

### A estratégia, em três ideias

**1. Testes de contrato parametrizados.** As mesmas asserções rodam contra `InMemoryBackend` e `PostgresBackend`. Isso prova que o comportamento observável é idêntico e impede que o backend em memória vire uma simplificação mentirosa.

**2. Concorrência verificada por contabilidade exata, não por "não deu erro".** Cada teste dispara dezenas de threads liberadas simultaneamente e confere o resultado no fim:

- 300 reservas concorrentes de 1 unidade sobre estoque 100 → **exatamente 100 aceitas, 200 recusadas**, `disponível = 0`
- 200 reservas de 7 unidades sobre estoque 1000 → **exatamente 142 aceitas** (⌊1000/7⌋), sobra < 7
- saídas + reservas + bloqueios disputando 30 unidades → a soma dos sucessos é **exatamente 30**
- 20 liberações concorrentes do mesmo hold → **1 sucesso, 19 recusas**; as unidades voltam **uma única vez**
- 30 "primeiras entradas" simultâneas do mesmo produto novo → **um único produto**, total 300
- carga aleatória com 16 threads × 60 operações → os contadores batem com o que cada thread acredita ter feito
- contenção: lock segurado por outra transação → **503 em < 3 s**, e **outro produto continua operando**

**3. Os testes foram validados por mutação.** Um teste de concorrência que passa não prova nada se também passaria com o código errado. Removi o lock pessimista dos dois backends (tirei o `FOR UPDATE` e neutralizei o `threading.Lock`) e rodei a suíte:

```
11 failed, 1 passed            # com o lock removido
assert 300 == 100              # 300 reservas aceitas sobre 100 unidades: oversell de 200
```

Com o código correto, os mesmos 12 testes passam. **Os testes detectam a ausência do lock** — é isso que os torna evidência, e não decoração.

Esse exercício expôs um problema real na própria suíte: na primeira versão, o teste de liberação dupla **passava mesmo sem lock**, porque o pool do PostgreSQL abria conexões sob demanda e as threads acabavam serializadas esperando conexão — a corrida nunca acontecia. Corrigi aquecendo o pool nos testes (`min_size == max_size`) e injetando um relógio deliberadamente lento (2 ms) que alarga a janela crítica sem tocar no código de produção. Depois disso, o mutante falha de forma consistente.

Outras garantias cobertas: rollback de produto **e** hold quando uma exceção sobe no meio da UoW; uma primeira entrada que falha não deixa produto fantasma; tentativa de gravar sem segurar o lock é recusada (`InvariantViolationError`); e uma sequência de 2.000 operações aleatórias com semente fixa é comparada passo a passo contra um modelo de referência independente.

---

## 8. Docker

**`Dockerfile`** — multi-stage com quatro estágios (`builder`, `runtime`, `test`, `release`):

- **Build cacheável:** dependências instaladas em estágio separado, antes de copiar o código. Alterar uma linha de Python não reinstala nada.
- **Imagem enxuta:** `python:3.12-slim`; pip, compiladores e caches ficam no estágio `builder`. `psycopg[binary]` dispensa toolchain de compilação no runtime.
- **Não-root:** usuário `app` (uid 10001), sem shell de login nem home.
- **`HEALTHCHECK`** batendo em `/health` com a stdlib — sem instalar `curl` só para isso.
- **Alvo de teste no mesmo arquivo:** o estágio `test` herda de `runtime`, então **os testes rodam contra a mesma imagem que vai para produção**, e não contra um ambiente parecido.
- **`.dockerignore`** evita `.git`, `.venv`, caches e `.env` no contexto de build.

**`docker-compose.yml`** — PostgreSQL com healthcheck + API, que só inicia quando o banco está pronto (`condition: service_healthy`). A API sobe com **`WEB_CONCURRENCY: 2`**, deliberadamente: dois processos independentes demonstram que a consistência vem do banco, não de um lock em memória de um único processo. O serviço `tests` fica atrás de um `profile`, para não subir junto com a aplicação.

> `docker compose config` foi validado nesta entrega. O build da imagem **não** pôde ser executado no ambiente em que preparei o case (não havia daemon Docker disponível), então o `Dockerfile` está revisado mas não executado — é a verificação pendente mais óbvia antes de confiar nele em CI.

---

## 9. Decisões técnicas e trade-offs

| Decisão | Alternativa | Por quê |
|---|---|---|
| `disponível` derivado | 4 contadores armazenados | Inconsistência vira impossibilidade aritmética |
| Agregado = 1 produto | Agregado por depósito/catálogo | Fronteira de consistência mínima → locks independentes por SKU → paralelismo |
| Contadores no agregado, holds à parte | Lista de holds dentro do agregado | Agregado O(1) mesmo com 100 mil reservas; sem carregar lista inteira a cada operação |
| Lock pessimista | Otimista / 2PC / UPDATE condicional | Contenção alta no mesmo SKU é o cenário dado; ver [seção 4](#4-concorrência-o-problema-e-a-solução) |
| Dois backends, um contrato | Só PostgreSQL | Permite rodar sem banco e **testar a mesma suíte duas vezes** |
| `Hold` imutável (`frozen`) | Mutação in-place | Liberar devolve novo objeto; impossível "liberar" sem passar pelo agregado |
| Holds nunca apagados | `DELETE` ao liberar | Auditoria + distinguir 404 de 409 |
| Reserva exige `request_id` | Campo opcional | O enunciado vincula reserva a uma solicitação; obrigatório evita reservas órfãs |
| Erros `code` + `details` | Só mensagem de texto | Cliente decide programaticamente; mensagem é para humano |
| Validação fora do lock | Dentro da seção crítica | Seção crítica mais curta = mais vazão |
| Endpoints síncronos | `async def` | Driver síncrono; threadpool do FastAPI dá backpressure natural |
| Relógio e id injetados | `datetime.now()` no domínio | Domínio determinístico; viabiliza o relógio lento dos testes de corrida |
| Schema criado no startup | Alembic | Pragmatismo no escopo do case; ver [seção 14](#14-o-que-eu-faria-diferente-em-produção-real) |
| Dependências pinadas | Versões abertas | Build reproduzível |

---

## 10. Premissas assumidas

1. **A primeira entrada cria o produto.** Não há cadastro separado; `POST /entries` em um id novo cria o produto zerado e credita a quantidade. Demais operações exigem produto existente (404). Em produção, o cadastro viria de um serviço de catálogo.
2. **Quantidades são inteiros positivos** (unidades discretas), com teto de 1 bilhão por operação — guarda contra entrada acidental absurda e overflow. Peso/volume exigiriam decimal e outra modelagem.
3. **Saída consome apenas o disponível.** Retirar unidades reservadas ou bloqueadas exigiria liberar antes — decisão de negócio explícita, não um efeito colateral.
4. **Reserva e bloqueio disputam o mesmo pool de disponíveis.** Não há prioridade entre eles; vence quem chega primeiro.
5. **Liberar uma reserva devolve ao disponível, não ao cliente.** Confirmar uma venda é `liberar` + `saída` (ou, em evolução, um `confirmar` atômico — ver [seção 14](#14-o-que-eu-faria-diferente-em-produção-real)).
6. **Um único depósito.** Não há dimensão de localização; adicioná-la mudaria a chave do agregado para (produto, depósito).
7. **Reservas não expiram** automaticamente — não há TTL nem varredura.
8. **Sem autenticação/autorização**, fora do escopo do case.
9. **Identificadores:** 1–64 caracteres `[A-Za-z0-9._:-]`, validados na borda.
10. **O chamador tolera 503 e repete.** Em contenção, falhar rápido é preferível a enfileirar.

---

## 11. Premissas de volumetria e capacidade

O enunciado não informa volumetria, então **dimensionei para o pior caso plausível** e deixo as premissas explícitas para serem contestadas:

| Premissa | Valor assumido | Implicação no desenho |
|---|---|---|
| Catálogo | 10⁴–10⁶ SKUs ativos | Lock por produto → paralelismo cresce com o catálogo |
| Carga agregada | centenas a poucos milhares de req/s | Um PostgreSQL bem dimensionado atende; não justifica sharding |
| **Distribuição** | **fortemente assimétrica (cauda longa)** | **Esta é a premissa que mais importa** — ver abaixo |
| Pico em SKU quente | 10²–10³ req/s no mesmo produto | Serialização por linha; lock timeout de 2 s evita colapso |
| Duração da seção crítica | < 5 ms (3 statements, sem I/O externo) | Teto teórico da ordem de 10²–10³ ops/s **por SKU** |
| Reservas ativas por produto | até 10⁵ | Por isso holds ficam fora do agregado |
| Leitura vs. escrita | leitura domina | Consultas não pegam lock; réplica de leitura é evolução natural |

**A cauda longa é o ponto central.** Em estoque real, a carga nunca se distribui por igual: na Black Friday, um punhado de SKUs concentra a maior parte das requisições, enquanto milhares ficam ociosos. Um lock global, ou mesmo por tabela, transformaria 10.000 produtos ociosos em vítimas da contenção de 3 produtos quentes. O lock por linha isola o problema onde ele realmente está. A consequência honesta: **a vazão de um único SKU tem teto** — e esse teto é inerente ao requisito de nunca vender o que não existe. Qualquer solução que ultrapasse esse teto o faz relaxando a garantia (reserva otimista com reconciliação) ou particionando o estoque (ver [seção 14](#14-o-que-eu-faria-diferente-em-produção-real)).

**Capacidade por processo:** `DB_POOL_MAX` (padrão 10) conexões, uma por operação em voo. Com `WEB_CONCURRENCY=2` são 20 conexões por contêiner — conta a fazer antes de escalar réplicas, porque `max_connections` do PostgreSQL é finito. Acima de ~4 réplicas, entra um pooler (PgBouncer em modo transaction).

**Nada disso é medido** — são estimativas analíticas a partir da estrutura do código. A primeira ação antes de ir a produção é um teste de carga que as confirme ou derrube ([seção 13](#13-os-5-aspectos-mais-importantes-para-produção)).

---

## 12. Limitações conhecidas

1. **O backend em memória só é correto em um processo.** Com múltiplas réplicas, `threading.Lock` não protege nada. Por isso `STOCK_BACKEND=postgres` é o padrão quando há `DATABASE_URL`, e o compose sobe PostgreSQL. Documentado no próprio módulo.
2. **Schema criado no startup**, não com migrações versionadas — adequado ao case, inadequado para produção (ver seção 14).
3. **Operações não são idempotentes.** Um retry de rede após timeout pode criar uma segunda reserva. Mitigação conhecida: chave de idempotência (seção 14).
4. **Sem expiração de reservas:** uma reserva esquecida imobiliza estoque indefinidamente.
5. **Sem paginação/listagem** de holds por produto (o índice `(product_id, status)` já existe para suportá-la).
6. **Sem observabilidade estruturada:** há logs, mas não métricas, tracing nem SLO.
7. **A imagem Docker não foi construída** no ambiente de preparação (sem daemon disponível); o compose foi validado, o build não.
8. **Volumetria não medida** — estimada analiticamente, sem teste de carga.
9. **Sem autenticação, rate limiting ou multi-tenancy.**
10. **Vazão limitada por SKU** sob contenção extrema — limite inerente à garantia de consistência forte.

---

## 13. Os 5 aspectos mais importantes para produção

**1. Consistência verificável sob concorrência.** É o requisito que define este domínio: oversell custa dinheiro, confiança e, muitas vezes, cancelamento manual. A garantia não vale pela intenção, mas pela prova — por isso há lock pessimista por produto, constraints no banco como segunda linha de defesa, e testes de concorrência que falham quando o lock é removido (verificado por mutação). Um sistema cuja correção não pode ser demonstrada é um sistema que ninguém pode alterar com segurança.

**2. Observabilidade orientada a negócio, não só a infraestrutura.** CPU saudável e estoque errado é o cenário mais caro: o problema circula por horas antes que alguém perceba. O que precisa ser instrumentado são os sinais que revelam dano real — taxa de `insufficient_stock` por SKU (demanda reprimida ou bug?), frequência de `lock_timeout` (contenção crescendo), p95/p99 do tempo de espera pelo lock, idade das reservas ativas, e uma **reconciliação periódica** comparando contadores com a soma dos holds ativos (a consulta já existe como teste de integração — em produção vira job com alerta). SLOs sobre essas métricas, com logs estruturados e `correlation_id` propagado, transformam incidentes de investigação em diagnóstico.

**3. Idempotência e comportamento correto sob falha parcial.** Em rede, "não recebi resposta" nunca significa "não aconteceu". Hoje um retry após timeout pode duplicar uma reserva — justamente a operação que o cliente mais repete. Idempotência (chave única por operação, resposta original devolvida em repetições) é o que permite ao cliente ter retry automático sem risco; sem ela, cada falha transitória vira trabalho manual de conciliação. Vale igualmente para a política de retry: só em `TransientError`, com backoff exponencial e jitter, teto de tentativas e deadline propagado — retry cego sobre um banco em dificuldade transforma lentidão em indisponibilidade.

**4. Evolução segura: migrações versionadas e compatibilidade entre versões.** Estoque é sistema que não pode parar, então todo deploy convive por minutos com duas versões da aplicação rodando contra o mesmo banco. Migrações versionadas (Alembic) com o padrão **expand → migrate → verify → contract** garantem que versão antiga e nova funcionem simultaneamente, e que exista caminho de volta. Mudança de schema é a classe de alteração com maior potencial destrutivo e menor reversibilidade — e a arquitetura em camadas serve a esse mesmo objetivo: com a regra isolada do SQL e do HTTP, trocar persistência ou adicionar uma regra não exige reescrever o resto.

**5. Capacidade comprovada por medição, com degradação previsível.** As premissas da [seção 11](#11-premissas-de-volumetria-e-capacidade) são estimativas, não fatos. Um teste de carga com **distribuição realista** (hot SKUs concentrando a carga, não acesso uniforme — que esconde exatamente o gargalo que importa) estabelece a linha de base e mostra onde o sistema quebra. Igualmente importante é **como** ele quebra: os timeouts e o pool limitado garantem que a saturação vire 503 rápido e localizado, em vez de fila crescente e colapso global. Um sistema que degrada de forma previsível pode ser operado; um que colapsa em cascata, não.

---

## 14. O que eu faria diferente em produção real

**Antes do primeiro deploy**

- **Migrações versionadas (Alembic)** substituindo `ensure_schema()` no startup, com rollback testado e o ciclo expand/contract.
- **Idempotência** via tabela `(idempotency_key, request_hash, response)` com `UNIQUE`, gravada na mesma transação da operação. Chave repetida devolve a resposta original em vez de executar de novo.
- **Observabilidade:** logs estruturados em JSON com `correlation_id`, métricas Prometheus (latência por operação, espera pelo lock, taxa de 409/503, conexões em uso) e tracing OpenTelemetry cobrindo a seção crítica. Dashboards e alertas ligados a SLO.
- **Teste de carga** com distribuição assimétrica realista, para confirmar ou derrubar a seção 11.
- **CI completo:** lint (`ruff`), formatação, type-check (`mypy --strict`), a suíte contra PostgreSQL real em serviço de CI, build da imagem, scan de vulnerabilidades (`trivy`), SBOM e imagem assinada. O build do Docker, que não pude executar aqui, entra nessa trilha.
- **Segurança:** autenticação (mTLS ou OIDC), autorização por operação, rate limiting por cliente e credenciais vindas de secret manager — nunca do compose.

**Evoluções de domínio**

- **Expiração de reservas:** TTL por reserva + job de varredura idempotente liberando as vencidas, com evento de notificação. Hoje uma reserva esquecida imobiliza estoque para sempre.
- **`confirmar` atômico** (`POST /reservations/{id}/confirm`): converte reserva em saída em uma única transação, eliminando a janela entre `liberar` e `saída`, na qual outra requisição pode roubar as unidades.
- **Histórico de movimentações** (append-only) como trilha de auditoria: hoje sabemos o saldo, não como se chegou nele. É o que responde "por que esse produto está com 3 unidades?" — pergunta inevitável em estoque.
- **Reserva parcial** (`allow_partial`), devolvendo o que foi possível reservar, quando o negócio preferir a venda parcial à recusa.
- **Eventos de domínio via transactional outbox:** publicar `StockReserved`/`StockReleased` gravando o evento na mesma transação e despachando por um worker. Evita o clássico "commitei no banco mas o evento se perdeu" sem recorrer a 2PC.

**Escala, se e quando a medição exigir**

- **Réplica de leitura** para consultas (já não pegam lock), atentando à defasagem de replicação em leitura-após-escrita.
- **Pooler (PgBouncer, modo transaction)** acima de ~4 réplicas.
- **Particionamento de estoque por SKU muito quente:** dividir o saldo em N "baldes" que podem ser travados independentemente, com reconciliação periódica. Aumenta a vazão de um SKU à custa de complexidade real — só com evidência de que o lock por linha virou o gargalo.
- **Dimensão de depósito/localização**, se o negócio exigir, mudando a chave do agregado para (produto, depósito) — o que, por sinal, *aumenta* o paralelismo.

---

## 15. Uso de IA

A documentação de uso de IA está em **[`docs/USO_DE_IA.md`](docs/USO_DE_IA.md)**: os principais prompts, onde a IA mais ajudou, o que eu rejeitei das sugestões dela e os erros que ela cometeu — incluindo um teste que passava mesmo com o código errado, encontrado por um experimento de mutação.

# Uso de Inteligência Artificial no desenvolvimento

Usei IA (Claude) como par técnico durante todo o case: para explorar alternativas de concorrência, gerar a primeira versão de módulos e, principalmente, **atacar o próprio trabalho em busca de furos**. Toda decisão de arquitetura passou por avaliação minha, e várias sugestões foram recusadas.

---

## 1. Principais prompts

### 1.1 Enquadramento inicial

> Atue como um Staff/Senior Software Engineer. Analise o case de controle de estoque e atue como meu parceiro técnico para desenharmos uma solução robusta, escalável e testável em Python. Como o descritivo não detalha a volumetria, projete para alta concorrência. Aborde estratégias avançadas de consistência de dados, como Lock Pessimista ou Two-Phase Commit, para garantir que não ocorram race conditions durante bloqueio, reserva e retirada de estoque.

**Por que esse prompt funcionou:** pedir um papel específico ("Staff/Senior") e nomear o problema real (*race conditions*, não "faça um CRUD") muda radicalmente a qualidade da resposta. Nomear duas estratégias concretas força a comparação em vez de uma escolha arbitrária.

### 1.2 Forçar a defesa de uma alternativa que eu suspeitava ser inadequada

> O enunciado do meu case sugere considerar Two-Phase Commit. Argumente a favor e contra aplicar 2PC aqui, considerando que existe um único banco de dados. Se 2PC for desnecessário, diga claramente e explique o que eu deveria responder se um entrevistador insistir nele.

**Resultado:** a IA foi direta em dizer que 2PC não se aplica — 2PC coordena **vários gerenciadores de recursos**, e aqui toda a unidade de trabalho (contadores + hold) cabe em **uma transação ACID local**, que já é atômica. Adicionar 2PC traria coordenador, fase de prepare, locks mais longos e risco de bloqueio na queda do coordenador, sem ganho de correção. Incorporei esse raciocínio à tabela comparativa do README, junto com a observação de que, se o estoque precisasse confirmar junto com outro sistema transacional, a recomendação seria **Saga com compensação** (a reserva já é um passo compensável: liberar) e não 2PC.

Este foi o prompt mais valioso do case: **pedir à IA que questione uma premissa do próprio enunciado**, em vez de obedecer.

### 1.3 Modelagem do domínio

> Modele o agregado de estoque com encapsulamento forte. Quero discutir: (a) guardar `disponível` como coluna ou derivá-lo de `total - reservado - bloqueado`; (b) manter a lista de holds dentro do agregado ou fora. Me dê o trade-off de cada um, não só a sua escolha.

Levou às duas decisões estruturais do projeto: **disponível derivado** (inconsistência vira impossibilidade aritmética) e **holds fora do agregado** (agregado O(1) mesmo com 100 mil reservas).

### 1.4 Projetar os testes a partir do que pode dar errado

> Liste os cenários de concorrência que podem corromper este estoque. Para cada um, escreva um teste pytest que falhe de forma determinística se a proteção for removida — e que verifique contabilidade exata no fim, não apenas ausência de exceção.

A exigência de "contabilidade exata" é o que separa um teste de concorrência útil de um teatro: "não lançou exceção" passa até com o código errado.

### 1.5 Validar os testes por mutação (o prompt que mais rendeu)

> Faça uma cópia do projeto, remova o lock pessimista dos dois backends (tire o `FOR UPDATE` e neutralize o `threading.Lock`) e rode a suíte de concorrência. Quais testes **não** falham? Esses são testes inúteis — explique por quê e corrija.

**Resultado na primeira execução: 9 falharam, 3 passaram.** Entre os que passaram estava justamente o teste de liberação dupla — ou seja, ele teria aprovado um código com bug. A análise do motivo gerou a correção descrita na seção 3.

### 1.6 Revisão adversarial

> Releia o backend PostgreSQL procurando por: locks liberados cedo demais, exceções que escapam sem rollback, erros do driver não traduzidos, e qualquer caminho em que a conexão volte ao pool em estado sujo.

Essa revisão apontou que `lock_timeout` precisava ter escopo de **transação** (`set_config(..., true)`) e não de sessão — caso contrário o ajuste vazaria para a próxima requisição que reutilizasse aquela conexão do pool. É um bug sutil, específico de pool, e difícil de notar em leitura casual.

---

## 2. Onde a IA mais ajudou

1. **Mapear o espaço de alternativas de concorrência rapidamente.** Lock pessimista, otimista, 2PC, `UPDATE` condicional, Serializable e lock distribuído — com prós, contras e modos de falha de cada um — em minutos. A decisão foi minha; o mapa veio pronto.
2. **Gerar testes de concorrência abrangentes.** Escrever cenários com barreira de largada, contabilidade exata e carga aleatória é trabalhoso e repetitivo. A IA produziu a base e eu ajustei o que estava frouxo.
3. **Revisão adversarial do próprio código.** Pedir "ache o que está errado aqui" foi consistentemente mais produtivo do que pedir mais código — e foi assim que apareceu o problema de escopo do `lock_timeout`.
4. **Executar e verificar de verdade.** Em vez de aceitar "os testes deveriam passar", a sessão subiu um PostgreSQL real, rodou a suíte três vezes (155 testes, estáveis) e executou o experimento de mutação. Afirmação sem execução não entrou no README.
5. **Documentação técnica densa.** Transformar decisões em texto explicativo com diagramas e tabelas de trade-off é onde a IA economiza mais tempo — desde que o conteúdo seja revisado linha a linha, porque é também onde ela mais "enfeita".

---

## 3. Sugestões da IA que modifiquei ou rejeitei

**a) Lock otimista como padrão.** A primeira proposta foi uma coluna `version` com retry. Rejeitei: sob contenção alta no mesmo SKU — exatamente o cenário do case — o otimista descobre o conflito só no fim e refaz todo o trabalho, desperdiçando até N−1 tentativas com N requisições concorrentes. Mantive o pessimista, que paga uma espera curta e faz o trabalho uma vez.

**b) Lock distribuído em Redis.** Sugerido como "mais escalável". Recusei: introduz um segundo sistema no caminho crítico, com lease, fencing token e split-brain para gerenciar, e cria uma janela entre soltar o lock e gravar no banco. O PostgreSQL oferece locks corretos **na mesma transação que grava os dados**.

**c) Duas classes separadas para reserva e bloqueio.** Teria duplicado todo o ciclo de vida (criar, consultar, liberar, auditar) por uma diferença puramente semântica. Unifiquei em `Hold` com discriminador `kind`, validado na liberação para que um id de reserva não libere um bloqueio.

**d) `DELETE` ao liberar um hold.** Apagar o registro destrói a trilha de auditoria e impede distinguir "nunca existiu" (404) de "já foi liberado" (409). Troquei por mudança de status com `released_at`.

**e) Endpoints `async def`.** Com driver síncrono, `async` não traz ganho e remove o backpressure natural do threadpool do FastAPI — corrotinas acumulariam em vez de o sistema rejeitar carga que não consegue atender. Mantive síncrono, por decisão explícita.

**f) Um endpoint genérico `/holds/{id}/release`.** Mais enxuto, porém permite liberar um bloqueio usando um id de reserva por engano. Separei em duas rotas com verificação de tipo.

**g) Mensagens de erro expondo detalhes internos.** A versão inicial devolvia a mensagem da `InvariantViolationError` ao cliente. Passei a logar internamente e responder uma mensagem genérica, com teste verificando que o detalhe não vaza.

---

## 4. Erros e limitações da IA que encontrei

**a) Teste de concorrência que passava mesmo com o código errado (o mais grave).** O teste de liberação dupla sobreviveu à remoção do lock. Motivo: o pool do PostgreSQL abria conexões sob demanda, então as 20 threads ficavam serializadas **esperando conexão** — a corrida que o teste pretendia provocar nunca acontecia. O teste parecia rigoroso e não valia nada.

Correções: aquecer o pool nos testes (`min_size == max_size`) e injetar um relógio deliberadamente lento (2 ms) que alarga a janela crítica **sem alterar o código de produção** — o relógio já era injetável por decisão de design. Depois disso, o mutante falha de forma consistente: 11 testes em vez de 9. A lição geral: **um teste de concorrência só tem valor depois de você vê-lo falhar com o código errado.**

**b) Ordem errada no tratamento de exceções do psycopg.** O código gerado capturava `psycopg.OperationalError` antes de `LockNotAvailable` e `QueryCanceled`. Como estas são subclasses daquela, o primeiro `except` engolia tudo e todo timeout de lock virava "banco indisponível" — erro operacional genérico em vez de contenção, com diagnóstico enganoso em produção. Reordenei: do mais específico para o mais genérico.

**c) `set_config` com escopo de sessão.** Como descrito na seção 1.6: sem o terceiro argumento `true`, `lock_timeout` e `statement_timeout` persistiriam na conexão após ela voltar ao pool, afetando requisições seguintes. Bug silencioso e específico de ambiente com pool.

**d) Teste de integração com transação mal gerenciada.** Um teste inseria uma linha e, no laço seguinte, dava `rollback()` após cada `UPDATE` inválido — desfazendo também o `INSERT` inicial. Falhou com "DID NOT RAISE CheckViolation", o que inicialmente parecia um problema nas constraints do banco, quando o defeito estava no teste. Corrigido com um `commit()` após a inserção.

**e) Documentação otimista.** Em rascunhos do README, a IA tendeu a afirmar que algo "garante consistência total" ou "está pronto para produção". Cortei: o Docker, por exemplo, **não pôde ser construído** no ambiente de preparação (sem daemon disponível) — o `docker-compose.yml` foi validado com `docker compose config`, mas a imagem não foi construída, e isso está declarado como limitação no README em vez de omitido.

---

## 5. Conclusão

A IA acelerou substancialmente a produção de código e documentação, mas o valor real apareceu quando **a usei contra o próprio trabalho**: o experimento de mutação, que revelou um teste inútil, e a revisão adversarial, que encontrou o bug de escopo no pool, foram mais decisivos do que qualquer código que ela tenha gerado do zero.

Os erros que ela cometeu têm um padrão reconhecível: **código plausível que falha em casos de borda de concorrência e de ciclo de vida de recursos** — ordem de captura de exceções, escopo de configuração em pool, transações em testes. São exatamente os pontos onde a revisão humana precisa ser mais cética, porque o código *parece* certo e os testes podem passar pelos motivos errados.

Entendo, sei explicar e assumo a responsabilidade por cada decisão e cada linha deste repositório.

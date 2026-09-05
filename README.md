# Telegram Ofertas Monitor

Bot que fica escutando os canais do Telegram em que você está e avisa quando aparece uma mensagem com alguma palavra-chave de interesse (ofertas, promoções, etc.), enviando um alerta direto pras suas Mensagens Salvas.

## Recursos

- 🔑 **Onboarding interativo** na primeira execução — pergunta tudo e salva a configuração sozinho
- 🚫 **Palavras-chave excludentes** — cancela o alerta se o texto tiver um termo indesejado, mesmo com uma keyword positiva batendo
- ⏱️ **Varredura de até 48h** pra canais sem histórico salvo, com opção de "Limpar histórico" pra forçar uma nova passada
- 🕒 **Data/hora original do post** em cada alerta, não só a hora em que foi detectado
- 🛡️ **Proteção contra flood do Telegram** — espaça os envios e reage automaticamente se o limite for atingido
- 📋 **Log persistente** e status ao vivo na bandeja do sistema (tooltip + menu com histórico)
- 🖱️ **Roda em segundo plano** com um duplo-clique, sem janela de console

## Como funciona

1. `telegram_monitor.pyw` usa [Telethon](https://docs.telethon.dev/) para conectar com a sua conta do Telegram (via API ID/Hash, não é um bot separado — ele lê como se fosse você mesmo).
2. Ao iniciar, revisita mensagens perdidas desde a última execução (controlado por `monitor_state.json`, um "até onde eu já vi" por canal) e depois passa a escutar novas mensagens em tempo real. Canal sem histórico salvo ainda (nunca visto, ou depois de "Limpar histórico") busca as últimas 48h em vez de ficar sem saber onde começar. A conversa "Mensagens Salvas" nunca é lida/escaneada — é só o destino dos alertas, nunca a origem.
3. Cada mensagem é comparada (case-insensitive) contra a lista de `KEYWORDS`. Se bater alguma, o texto também é checado contra `EXCLUDE_KEYWORDS`: se qualquer excludente aparecer, o alerta é cancelado mesmo com uma keyword positiva batendo (ex.: quer "iphone" mas não "iphone usado").
4. Quando bate alguma palavra-chave (e nenhuma excludente), envia um alerta formatado (canal, termo encontrado, **data/hora em que foi postado no canal**, link da mensagem, trecho do conteúdo) para o chat "Mensagens Salvas" (`me`) e dispara uma notificação desktop, se `pystray`/`plyer` estiverem instalados. Os envios são espaçados (mínimo 3s entre um e outro) pra não disparar o limite de flood do Telegram quando muitos alertas batem de uma vez (ex.: depois de "Limpar histórico"); se mesmo assim o limite for atingido, o monitor espera o tempo pedido pelo Telegram e tenta reenviar automaticamente uma vez.
5. Roda com um ícone na bandeja do sistema (opcional). Passar o mouse por cima mostra o status (conectado desde quando, quantos alertas nesta sessão, resumo do último). O menu (clique direito) tem "Ver histórico" (abre o `monitor.log` completo), "Limpar histórico" (zera o progresso salvo e refaz a varredura das últimas 48h em todos os canais na hora) e "Sair".
6. Assim que conecta com sucesso, dispara uma notificação desktop confirmando que está ativo e ouvindo.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
```

Não precisa criar `.env` na mão: na primeira execução (`python telegram_monitor.pyw`, rodando num terminal de verdade — não dê duplo-clique no `.pyw` ainda), se não encontrar `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` configurados, o próprio script pergunta tudo interativamente:

- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` — obtidos em https://my.telegram.org.
- `KEYWORDS` — palavras-chave que disparam alerta, separadas por vírgula.
- `EXCLUDE_KEYWORDS` — palavras-chave que cancelam o alerta mesmo se uma `KEYWORD` bateu (opcional).
- `CHANNELS` — usernames (sem `@`) ou IDs dos canais a monitorar, separados por vírgula. Deixe vazio para monitorar todos os canais que você segue.

No final, pergunta onde salvar: como variável de ambiente do usuário do Windows (recomendado — fica fora da pasta do projeto, sobrevive a um `git clone` novo) ou num `.env` local. Pra mudar algo depois, é só editar a variável de ambiente (ou o `.env`, se tiver escolhido essa opção) e reiniciar o script — ou apagar as variáveis pra rodar o assistente de novo.

Se preferir configurar manualmente sem o assistente, copie `.env.example` para `.env` e preencha os mesmos campos.

## Uso

```bash
python telegram_monitor.pyw
```

Na primeira execução, o Telethon pede autenticação (número de telefone + código recebido no Telegram) e cria `monitor_session.session` — esse arquivo guarda a sessão autenticada e funciona como uma chave de acesso à sua conta, **nunca compartilhe ou versione**.

Para rodar em segundo plano sem janela de console, depois que a configuração inicial já tiver sido feita pelo menos uma vez (o assistente precisa de terminal, por causa do `input()`), dê duplo-clique em **`Iniciar Monitor.vbs`** — ele chama o `pythonw.exe` do `.venv` do projeto (não o Python global) de forma totalmente invisível, sem piscar console nenhum. Pode até criar um atalho dele na Área de Trabalho ou colocar na pasta de Inicialização do Windows (`shell:startup`) pra subir junto com o PC.

## Arquivos gerados (não versionados)

- `.env` — só existe se você optou por essa forma de configuração; caso contrário as credenciais ficam em variável de ambiente do Windows.
- `monitor_session.session` — sessão autenticada do Telethon.
- `monitor_state.json` — último ID de mensagem processado por chat, para não reprocessar/perder mensagens entre execuções.
- `monitor.log` (+ `.log.1`, `.log.2`) — log com tudo que o monitor fez (conexão, alertas enviados, ignorados, erros). Como ele roda sem console (`pythonw`), esse arquivo é a única forma de ver o que aconteceu — abra pelo menu da bandeja ("Ver histórico") ou direto num editor de texto. Gira automaticamente por tamanho (não cresce pra sempre).

## Notas

- Projeto pessoal para uso próprio — respeite os Termos de Serviço do Telegram; use com moderação para não levar rate limit/flood wait na conta.
- Variável de ambiente definida via assistente só é visível em processos abertos **depois** da configuração — feche e abra o terminal (ou reinicie o PC, no caso de apps que já estavam rodando) antes de reclamar que "não pegou".
- Se rodar e parecer que "não fez nada", confira o `monitor.log` antes de mais nada — é o único jeito de saber se conectou, se deu erro, ou se só não bateu nenhuma palavra-chave ainda. Se o log mostrar que conectou normalmente mas nada bateu, o `monitor_state.json` provavelmente já está "em dia" com tudo que existe nos canais — use "Limpar histórico" no menu da bandeja pra forçar uma nova varredura das últimas 48h.

## Licença

MIT — veja [LICENSE](LICENSE).

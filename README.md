# DHL Express para Odoo 18

Conector da [DHL Express](https://www.dhl.com) sobre a MyDHL API: cotação no carrinho, criação
do envio com etiqueta, fatura comercial e declaração aduaneira, coleta opcional e rastreamento.

Branches: `18.0` (este) e `17.0` (versão anterior, nunca validada contra a API real).

## Por que não se chama `delivery_dhl`

A Odoo mantém um conector oficial de DHL, mas ele vive no **Enterprise**. No repositório
`odoo/odoo` o único conector de transportadora que existe em Community é o
`delivery_mondialrelay`. Numa base com Enterprise instalado, um módulo chamado `delivery_dhl`
colidiria com o oficial — daí o nome `delivery_dhl_express`.

---

## Instalação

Depende de `stock_delivery` e da biblioteca `requests`. Pelo Doodba (é assim que entra na
imagem `odoo-template`):

```yaml
# custom/src/repos.yaml
./odoo-delivery-dhl:
  defaults:
    depth: $DEPTH_DEFAULT
  remotes:
    evolars: https://github.com/evolars/odoo-delivery-dhl.git
  target: evolars $ODOO_VERSION
  merges:
    - evolars $ODOO_VERSION
```

## Credenciais

Exige **conta ativa na DHL Express** (conta de exportação). Com ela, o cadastro no
[portal do desenvolvedor](https://developer.dhl.com/api-reference/dhl-express-mydhl-api)
pedindo acesso à *MyDHL API* devolve a **chave e o segredo da API**, normalmente em dois e-mails:
um liberando o **teste** e outro a **produção**. O ambiente de teste tem limite de **500
chamadas por dia** e só rastreia números de exemplo da DHL, não os envios criados nele.

O ambiente segue o campo *Ambiente* do método de entrega — fora de produção, as chamadas vão
para `/mydhlapi/test`. **Testar conexão** confere a credencial e se a DHL faz coleta no endereço
de saída (`GET /address-validate`), sem criar nada.

### Antes de a conta existir

Ligue o **modo simulação**: o método cota por um valor fixo local, sem chamar a DHL. Despachar
fica bloqueado.

---

## Configuração

Inventário → Configuração → Métodos de Entrega → novo método, provedor **DHL Express**:

| Campo | O que é |
|---|---|
| Chave / Segredo da API | Basic Auth da MyDHL API |
| Número da Conta DHL | a conta de exportação (`shipper`), obrigatória para criar envio |
| Código do Produto | vazio cota o mais barato; preenchido fixa o serviço (ex.: `P`) |
| Incoterm | DAP (padrão) ou DDP |
| Só internacional | esconde o método quando o destino é o país de onde o pacote sai |
| Dias até o despacho | a data planejada que a DHL cota (dias úteis, até 9) |
| Código HS padrão | para produto sem `hs_code` (ex.: `490199`, livro impresso) |
| Caixas disponíveis / Embalagem padrão | como na Loggi: entra a menor caixa que comporta o pedido |
| Formato da etiqueta | 8 × 4 pol (térmica), A6 ou A4 |
| Fatura comercial pela DHL | pede o PDF da commercial invoice junto com a etiqueta |
| Pedir coleta | agenda a coleta ao criar o envio (horário limite e local) |
| Exigir NF-e autorizada | exportador brasileiro com IE não despacha sem NF-e (ligado por padrão) |

---

## Incoterm e o checkout: leia isto

O padrão é **DAP** — o destinatário paga imposto de importação e desembaraço na entrega.

A cotação devolve um `warning_message` dizendo isso, mas **o checkout do Odoo 18 não mostra o
aviso da cotação**: só a *Descrição* do método no site (`website_description`), embaixo do nome.
Ponha o aviso ali. Sem ele o comprador descobre a cobrança na porta de casa, recusa o pacote, e
a devolução sai por conta do remetente — frete pago duas vezes e venda perdida.

Para a União Europeia isso pesa mais desde 1º de julho de 2026, quando acabou a isenção de
direitos aduaneiros para remessas de até €150, substituída por uma taxa fixa de €3 por remessa,
somada ao IVA do país de destino.

Com **DDP** o remetente assume os impostos e o aviso não aparece.

---

## Como funciona

Toda chamada leva Basic Auth e o header **`x-version: 3.3.2`**, obrigatório na especificação.

**Cotação** (`POST /rates`) manda remetente e destinatário (país, cidade, CEP, estado), os volumes,
a conta e o valor declarado. Os volumes vêm do empacotamento do próprio Odoo, com as medidas
da embalagem convertidas da unidade do Odoo (mm) para cm. `isCustomsDeclarable` é verdadeiro
quando o país de destino difere do de origem.

A data planejada vai na **hora local do remetente com o fuso explícito**
(`2026-10-05T10:00:00GMT-03:00`), pulando fim de semana: a DHL recusa data passada ou mais de
10 dias à frente.

A DHL devolve o preço em até três moedas; o módulo usa a **faturada** (`currencyType: BILLC`) e
converte para a moeda da empresa se forem diferentes. Sem código de produto configurado, cota a
opção mais barata; com um código fixado e indisponível para o destino, cota a que houver.

**Falha na cotação não estoura.** Devolve `success: False` com a mensagem, e o checkout segue
com os outros métodos (timeout de 15 s).

**Envio** (`POST /shipments`) na validação da entrega. Antes, cota de novo os volumes da
entrega: dá o produto (obrigatório no envio) e o custo real. O payload leva:

* remetente com **CNPJ** (`registrationNumbers`, código `CNP` da DHL para CNPJ/CPF brasileiro),
  destinatário com nome, telefone (obrigatório) e e-mail, tipo `business` ou `private`;
* `declaredValue` e `declaredValueCurrency` (exigidos pela DHL em envio declarável);
* declaração aduaneira: uma linha por produto com código HS (do produto, ou o padrão do método),
  preço **unitário**, quantidade em `PCS`, país de fabricação e peso **total da linha** (a DHL
  não multiplica); motivo `sale`/`permanent`; `placeOfIncoterm` é a cidade de destino;
* a NF-e de exportação (ver abaixo).

A etiqueta (e a fatura comercial, se pedida) volta em PDF base64 e é anexada à entrega, junto
com o DANFE e o XML da NF-e. Não há reimpressão de etiqueta pela API.

### NF-e de exportação

Pelo manual do MyDHL+ da DHL Brasil, empresa com Inscrição Estadual só envia produto com nota
fiscal (sem nota, só isento de IE ou pessoa física), e a Receita cruza a NF-e com a fatura
comercial. Com *Exigir NF-e autorizada* (padrão), remetente brasileiro com IE **não despacha
sem NF-e autorizada na venda** (localização fiscal OCA): a validação da entrega mostra o aviso.

A MyDHL API não tem campo de NF-e (conferido até a 3.3.2: o único tratamento brasileiro é o
`CNP` no remetente). A nota vai por todos os meios que a API oferece:

* chave impressa na etiqueta (`imageOptions[label].labelCustomerDataText`);
* fatura comercial com o **número e a data da NF-e** (`exportDeclaration.invoice`);
* chave nas observações da declaração (`exportDeclaration.remarks`);
* DANFE e XML autorizados anexados à entrega, para o DANFE seguir com o pacote.

**Rastreio**: link `https://www.dhl.com/br-pt/home/rastreamento.html?tracking-id=<AWB>&submit=1`
no portal; botão *Atualizar rastreio* registra o último evento na entrega.

**Cancelamento**: a MyDHL API não anula conhecimento, e nem precisa. Pelos termos da API,
emitir o conhecimento não é contrato de transporte, que só nasce quando o pacote é entregue ou
coletado; o que os termos permitem cobrar é a coleta agendada sem pacote. Ao cancelar o envio na
entrega, o módulo cancela a coleta na DHL (`DELETE /pickups/{número}`), marca a etiqueta como
**CANCELADA** para ninguém imprimir e libera a entrega para ser despachada de novo.

### Exportação a partir do Brasil

Pelas regras da DHL Brasil, até **USD 1.000** o despacho é simplificado (courier): CNPJ do
exportador, fatura comercial com o frete e a NF-e de exportação. Acima disso, ou volume acima de
120 × 80 × 80 cm, o despacho é formal, com DU-E e despachante — fora do que este módulo faz.

### Erros

A DHL responde no formato da RFC 7807 e detalha o campo recusado em `additionalDetails`; a
mensagem mostrada junta as duas coisas.

---

## Testes

```bash
docker run --rm --network <rede> \
  -v "$PWD/delivery_dhl_express":/opt/odoo/custom/src/private/delivery_dhl_express:ro \
  -e PGHOST=<db> -e PGUSER=odoo -e PGPASSWORD=odoo -e WAIT_DB=true \
  ghcr.io/evolars/odoo-template:18.0 \
  odoo -d dhl_test -i delivery_dhl_express --test-enable --test-tags /delivery_dhl_express \
  --stop-after-init --without-demo=all --http-port 8099
```

35 testes, nenhum tocando a rede: Basic Auth e `x-version`, formato de erro, cotação (payload,
fuso da data, moeda faturada e conversão, produto fixo e fallback, aviso DAP, só internacional,
falhas sem exceção, simulação), teste de conexão, envio (payload completo, CNPJ, declaração,
documentos anexados, telefone e HS obrigatórios, coleta), NF-e (obrigatória com IE, na etiqueta,
na fatura comercial e nas observações), cancelamento (coleta, etiqueta, novo despacho) e rastreio.

---

## Estado

Escrito contra a especificação OpenAPI oficial da MyDHL API **3.3.2** (06/09/2026), conferida
em 02/10/2026. **Ainda não validado contra a API real** — falta a conta. Pontos a observar na
primeira chamada: códigos de produto da conta e da rota, e se a validação de endereço aceita o
formato de rua brasileiro.

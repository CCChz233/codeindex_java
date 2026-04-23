# 27 个就绪索引：InfCode 远程 MCP（逐条加载）

每条以下为 **独立一份** 配置，可直接整段复制到 InfCode（一次添加一个 MCP）。

**网关基址** 默认为 `http://45.78.221.74:8765`；若不同请替换各 JSON 中的 URL 前缀。

清单来源：`workspace/manifests/test_java_agent_manifest_size_ge_100000.targets.json`（`skip: false` 且不在 `skipped_targets`）。

`mcpServers[0].name` 由 slug 生成（`_` 与 `.` 均改为 `-`），仅用于客户端展示；**远端路径仍以 URL 中的 `slug` 为准**。

---

## 1. `OpenAPITools/openapi-generator` @ `473343ff` — `OpenAPITools_openapi-generator_473343ff94d11a3f36507cde2b371d2165df6cb8`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-OpenAPITools-openapi-generator-473343ff94d11a3f36507cde2b371d2165df6cb8",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/OpenAPITools_openapi-generator_473343ff94d11a3f36507cde2b371d2165df6cb8",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 2. `OpenRefine/OpenRefine` @ `02c80d0e` — `OpenRefine_OpenRefine_02c80d0e49bb11c0b21d62d47a5ca13cb11048a9`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-OpenRefine-OpenRefine-02c80d0e49bb11c0b21d62d47a5ca13cb11048a9",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/OpenRefine_OpenRefine_02c80d0e49bb11c0b21d62d47a5ca13cb11048a9",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 3. `OpenRefine/OpenRefine` @ `a4612206` — `OpenRefine_OpenRefine_a461220681f024ab7b0836934b1176de3ab24d28`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-OpenRefine-OpenRefine-a461220681f024ab7b0836934b1176de3ab24d28",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/OpenRefine_OpenRefine_a461220681f024ab7b0836934b1176de3ab24d28",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 4. `TimefoldAI/timefold-solver` @ `1a86e96b` — `TimefoldAI_timefold-solver_1a86e96b4607a71b7d7874def546f4ade1b71d06`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-TimefoldAI-timefold-solver-1a86e96b4607a71b7d7874def546f4ade1b71d06",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/TimefoldAI_timefold-solver_1a86e96b4607a71b7d7874def546f4ade1b71d06",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 5. `TimefoldAI/timefold-solver` @ `728dc36e` — `TimefoldAI_timefold-solver_728dc36e29c2c1a143208ac22c7f95054f8bc4d6`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-TimefoldAI-timefold-solver-728dc36e29c2c1a143208ac22c7f95054f8bc4d6",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/TimefoldAI_timefold-solver_728dc36e29c2c1a143208ac22c7f95054f8bc4d6",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 6. `apache/pinot` @ `f0d29a06` — `apache_pinot_f0d29a069587e091dbe7db8ad603afd2f72158fe`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-apache-pinot-f0d29a069587e091dbe7db8ad603afd2f72158fe",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/apache_pinot_f0d29a069587e091dbe7db8ad603afd2f72158fe",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 7. `dapr/java-sdk` @ `dcaca773` — `dapr_java-sdk_dcaca773b3864d815e7796fc2460bcec360e5e49`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-dapr-java-sdk-dcaca773b3864d815e7796fc2460bcec360e5e49",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/dapr_java-sdk_dcaca773b3864d815e7796fc2460bcec360e5e49",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 8. `georchestra/georchestra` @ `5a89214d` — `georchestra_georchestra_5a89214d7fb5825454c2af4ef5107fa0ce57c562`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-georchestra-georchestra-5a89214d7fb5825454c2af4ef5107fa0ce57c562",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/georchestra_georchestra_5a89214d7fb5825454c2af4ef5107fa0ce57c562",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 9. `hapifhir/hapi-fhir` @ `844649dc` — `hapifhir_hapi-fhir_844649dc061e923650fff02429437449d70805a2`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-hapifhir-hapi-fhir-844649dc061e923650fff02429437449d70805a2",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/hapifhir_hapi-fhir_844649dc061e923650fff02429437449d70805a2",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 10. `helidon-io/helidon` @ `74d06e72` — `helidon-io_helidon_74d06e72ec9af45d3a4fbecd72c369596d658b53`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-helidon-io-helidon-74d06e72ec9af45d3a4fbecd72c369596d658b53",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/helidon-io_helidon_74d06e72ec9af45d3a4fbecd72c369596d658b53",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 11. `helidon-io/helidon` @ `fe01e138` — `helidon-io_helidon_fe01e1385351dc464f33d8af19ee66454d5a86c6`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-helidon-io-helidon-fe01e1385351dc464f33d8af19ee66454d5a86c6",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/helidon-io_helidon_fe01e1385351dc464f33d8af19ee66454d5a86c6",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 12. `jetty/jetty.project` @ `639e2878` — `jetty_jetty.project_639e287866970654c31faa9bece4c289a8ba36e7`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-jetty-jetty-project-639e287866970654c31faa9bece4c289a8ba36e7",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/jetty_jetty.project_639e287866970654c31faa9bece4c289a8ba36e7",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 13. `jetty/jetty.project` @ `a607ec6a` — `jetty_jetty.project_a607ec6af5560eea444399989515341524442091`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-jetty-jetty-project-a607ec6af5560eea444399989515341524442091",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/jetty_jetty.project_a607ec6af5560eea444399989515341524442091",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 14. `keycloak/keycloak` @ `1eba0221` — `keycloak_keycloak_1eba022149550c3c94cd784910c605a7e1956a5f`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-keycloak-keycloak-1eba022149550c3c94cd784910c605a7e1956a5f",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/keycloak_keycloak_1eba022149550c3c94cd784910c605a7e1956a5f",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 15. `keycloak/keycloak` @ `236d2f9f` — `keycloak_keycloak_236d2f9f62ee41c641988cd2931f4b85a58d0f54`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-keycloak-keycloak-236d2f9f62ee41c641988cd2931f4b85a58d0f54",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/keycloak_keycloak_236d2f9f62ee41c641988cd2931f4b85a58d0f54",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 16. `keycloak/keycloak` @ `5387aef0` — `keycloak_keycloak_5387aef0fa727ea5cae4816f682ec72798fabaa4`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-keycloak-keycloak-5387aef0fa727ea5cae4816f682ec72798fabaa4",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/keycloak_keycloak_5387aef0fa727ea5cae4816f682ec72798fabaa4",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 17. `keycloak/keycloak` @ `b997d050` — `keycloak_keycloak_b997d0506f042cf32eb90bf36159e113fd5c2e21`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-keycloak-keycloak-b997d0506f042cf32eb90bf36159e113fd5c2e21",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/keycloak_keycloak_b997d0506f042cf32eb90bf36159e113fd5c2e21",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 18. `ls1intum/Artemis` @ `7364749a` — `ls1intum_Artemis_7364749a8de08befd5f96e9dfecf6d13e241944a`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-ls1intum-Artemis-7364749a8de08befd5f96e9dfecf6d13e241944a",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/ls1intum_Artemis_7364749a8de08befd5f96e9dfecf6d13e241944a",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 19. `netty/netty` @ `966af01e` — `netty_netty_966af01e78eaecc122cf61f917abcfacc1f95756`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-netty-netty-966af01e78eaecc122cf61f917abcfacc1f95756",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/netty_netty_966af01e78eaecc122cf61f917abcfacc1f95756",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 20. `nom-tam-fits/nom-tam-fits` @ `ba91e013` — `nom-tam-fits_nom-tam-fits_ba91e0138a3dee2264eacb127d5827fc0ad38d49`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-nom-tam-fits-nom-tam-fits-ba91e0138a3dee2264eacb127d5827fc0ad38d49",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/nom-tam-fits_nom-tam-fits_ba91e0138a3dee2264eacb127d5827fc0ad38d49",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 21. `quarkusio/quarkus` @ `3280e471` — `quarkusio_quarkus_3280e471d2107cbcde0b8e8964cd38421918ac27`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-quarkusio-quarkus-3280e471d2107cbcde0b8e8964cd38421918ac27",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/quarkusio_quarkus_3280e471d2107cbcde0b8e8964cd38421918ac27",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 22. `quarkusio/quarkus` @ `5be22575` — `quarkusio_quarkus_5be22575c7b027a8dde751dd34ea408acb43c6f6`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-quarkusio-quarkus-5be22575c7b027a8dde751dd34ea408acb43c6f6",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/quarkusio_quarkus_5be22575c7b027a8dde751dd34ea408acb43c6f6",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 23. `trinodb/trino` @ `98ea5761` — `trinodb_trino_98ea57616550a584e75155967f0f3b4e3e2b2fa8`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-trinodb-trino-98ea57616550a584e75155967f0f3b4e3e2b2fa8",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/trinodb_trino_98ea57616550a584e75155967f0f3b4e3e2b2fa8",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 24. `vaadin/flow` @ `7a51e5b8` — `vaadin_flow_7a51e5b8e01673338ba6e9783a66546b9a5f2ddc`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-vaadin-flow-7a51e5b8e01673338ba6e9783a66546b9a5f2ddc",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/vaadin_flow_7a51e5b8e01673338ba6e9783a66546b9a5f2ddc",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 25. `vaadin/flow` @ `97aa4590` — `vaadin_flow_97aa4590f3d601d73ae22fd833091809149ac1c1`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-vaadin-flow-97aa4590f3d601d73ae22fd833091809149ac1c1",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/vaadin_flow_97aa4590f3d601d73ae22fd833091809149ac1c1",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 26. `vaadin/flow` @ `a553b6bf` — `vaadin_flow_a553b6bf47cd6ea0d28c2a1bb049fe65ebd10cbc`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-vaadin-flow-a553b6bf47cd6ea0d28c2a1bb049fe65ebd10cbc",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/vaadin_flow_a553b6bf47cd6ea0d28c2a1bb049fe65ebd10cbc",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```

## 27. `vaadin/hilla` @ `56ec9926` — `vaadin_hilla_56ec9926df65eacca465adbfd83f55fc4f704f18`

```json
{
  "name": "New MCP server",
  "version": "0.0.1",
  "schema": "v1",
  "mcpServers": [
    {
      "name": "codeindex-vaadin-hilla-56ec9926df65eacca465adbfd83f55fc4f704f18",
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://45.78.221.74:8765/mcp/vaadin_hilla_56ec9926df65eacca465adbfd83f55fc4f704f18",
        "--transport",
        "http-only",
        "--allow-http"
      ]
    }
  ]
}
```


---
updatedAt: 2026-05-19T19:48:00.000Z
agentTools:
  projectIndex: https://developers.familysearch.org/main/llms.txt
---

# Read Person

Retrieve a single Person by the personId including the names, gender, and facts of the person.

# OpenAPI definition

```json
{
  "openapi": "3.0.1",
  "info": {
    "title": "Family Tree Endpoints",
    "description": "The FamilySearch API is a RESTful Web service that can be used to enable your apps to read and write data in various FamilySearch resources.",
    "contact": {
      "name": "FamilySearch Platform API Team",
      "url": "https://www.familysearch.org/developers",
      "email": "devsupport@familysearch.org"
    },
    "license": {
      "name": "Apache 2.0",
      "url": "https://www.apache.org/licenses/LICENSE-2.0"
    },
    "version": "1.0"
  },
  "servers": [
    {
      "url": "https://apibeta.familysearch.org/platform",
      "description": "Beta Server"
    }
  ],
  "tags": [
    {
      "name": "Persons"
    }
  ],
  "paths": {
    "/tree/persons/{pid}": {
      "get": {
        "tags": [
          "Persons"
        ],
        "summary": "Read Person",
        "description": "Retrieve a single Person by the personId including the names, gender, and facts of the person.",
        "operationId": "readPerson",
        "parameters": [
          {
            "name": "pid",
            "in": "path",
            "description": "The id of the person to retrieve. Can be a person from the main FamilySearch tree or from a user tree. To find an example person ID, see <a href=\"/reference/searchtreepersons\" target=\"_blank\">Search Tree Persons</a>.",
            "required": true,
            "schema": {
              "type": "string"
            },
            "example": "LZJW-C31"
          },
          {
            "name": "Accept",
            "in": "header",
            "description": "Specifies the media type(s) that the client is willing to accept in the response.",
            "required": true,
            "schema": {
              "type": "string",
              "enum": [
                "application/x-fs-v1+json",
                "application/x-gedcomx-v1+json",
                "application/json",
                "application/x-fs-v1+xml",
                "application/x-gedcomx-v1+xml",
                "application/xml"
              ]
            },
            "example": "application/x-fs-v1+json"
          },
          {
            "name": "relatives",
            "in": "query",
            "description": "True if related persons (all parents, all children, all spouses) should be included in the response; false otherwise.",
            "schema": {
              "type": "boolean"
            }
          },
          {
            "name": "sourceDescriptions",
            "in": "query",
            "description": "True if the call will attempt to include source descriptions; false otherwise.",
            "schema": {
              "type": "boolean"
            }
          },
          {
            "name": "failOnError",
            "in": "query",
            "description": "True if call is to fail on sources read error; false otherwise (will return person without failed sources).",
            "schema": {
              "type": "boolean"
            }
          }
        ],
        "requestBody": {
          "content": {
            "*/*": {}
          }
        },
        "responses": {
          "200": {
            "description": "The read was successful. See <a href=\"https://developers.familysearch.org/main/docs/json-schema#person\" target=\"_blank\">Person Schema (JSON)</a>"
          },
          "301": {
            "description": "If the requested person has been merged into another person.  A Location header is included with the URI of the person that this person was merged into."
          },
          "404": {
            "description": "If the requested person is not found."
          },
          "410": {
            "description": "If the requested person has been deleted."
          },
          "429": {
            "description": "The request was throttled."
          }
        },
        "security": [
          {
            "bearerAuth": []
          }
        ]
      }
    }
  },
  "components": {
    "securitySchemes": {
      "bearerAuth": {
        "type": "http",
        "scheme": "bearer"
      }
    }
  }
}
```
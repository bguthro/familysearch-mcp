---
updatedAt: 2026-05-19T19:48:00.000Z
agentTools:
  projectIndex: https://developers.familysearch.org/main/llms.txt
---

# Create Person

Create a person.

[Example](https://developers.familysearch.org/main/docs/create-person)

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
    "/tree/persons": {
      "post": {
        "tags": [
          "Persons"
        ],
        "summary": "Create Person",
        "description": "Create a person.",
        "operationId": "createPerson",
        "parameters": [
          {
            "name": "Content-Type",
            "in": "header",
            "description": "Specifies the media type of the entity-body sent to the server in the request.",
            "required": true,
            "schema": {
              "type": "string",
              "enum": [
                "application/x-fs-v1+json",
                "application/x-gedcomx-v1+json",
                "application/x-fs-v1+xml",
                "application/x-gedcomx-v1+xml"
              ]
            }
          },
          {
            "name": "X-Reason",
            "in": "header",
            "description": "The reason for creating the person.",
            "schema": {
              "type": "string"
            }
          }
        ],
        "requestBody": {
          "description": "The person to be created.",
          "content": {
            "application/x-gedcomx-v1+json": {
              "schema": {
                "$ref": "#/components/schemas/createPerson"
              }
            }
          },
          "required": true
        },
        "responses": {
          "201": {
            "description": "The creation of the person was successful. Expect a location header specifying the link to the created person."
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
    },
    "schemas": {
      "createPerson": {
        "type": "object",
        "properties": {
          "persons": {
            "type": "array",
            "description": "Array containing exactly one person to create. See <a href=\"https://developers.familysearch.org/main/docs/json-schema#person\" target=\"_blank\">Person Schema (JSON)</a>",
            "minItems": 1,
            "maxItems": 1,
            "items": {
              "type": "object",
              "properties": {
                "living": {
                  "type": "boolean",
                  "description": "Whether the person is living. Set to false for deceased persons.",
                  "example": false
                },
                "gender": {
                  "type": "object",
                  "description": "The gender of the person",
                  "properties": {
                    "type": {
                      "type": "string",
                      "description": "The type URI for the gender",
                      "example": "http://gedcomx.org/Male"
                    }
                  }
                },
                "names": {
                  "type": "array",
                  "description": "Array of names for the person. Do not include id fields when creating a new person.",
                  "items": {
                    "type": "object",
                    "properties": {
                      "type": {
                        "type": "string",
                        "description": "The type of name",
                        "example": "http://gedcomx.org/BirthName"
                      },
                      "preferred": {
                        "type": "boolean",
                        "description": "Whether this is the preferred name",
                        "example": true
                      },
                      "nameForms": {
                        "type": "array",
                        "description": "Array of name forms representing the name in different languages or scripts",
                        "items": {
                          "type": "object",
                          "properties": {
                            "fullText": {
                              "type": "string",
                              "description": "The full text of the name",
                              "example": "John Smith"
                            },
                            "parts": {
                              "type": "array",
                              "description": "Array of name parts (given name, surname, etc.)",
                              "items": {
                                "type": "object",
                                "properties": {
                                  "type": {
                                    "type": "string",
                                    "description": "The type of name part",
                                    "example": "http://gedcomx.org/Given"
                                  },
                                  "value": {
                                    "type": "string",
                                    "description": "The value of the name part",
                                    "example": "John"
                                  }
                                }
                              }
                            }
                          }
                        }
                      }
                    }
                  }
                },
                "facts": {
                  "type": "array",
                  "description": "Array of facts about the person (birth, death, residence, etc.). Do not include id fields when creating a new person.",
                  "items": {
                    "type": "object",
                    "properties": {
                      "type": {
                        "type": "string",
                        "description": "The type of fact",
                        "example": "http://gedcomx.org/Birth"
                      },
                      "date": {
                        "type": "object",
                        "description": "The date of the fact",
                        "properties": {
                          "original": {
                            "type": "string",
                            "description": "The original date string as entered",
                            "example": "3 Apr 1836"
                          },
                          "formal": {
                            "type": "string",
                            "description": "The formal standardized date (optional, will be calculated if omitted)",
                            "example": "+1836-04-03"
                          }
                        }
                      },
                      "place": {
                        "type": "object",
                        "description": "The place of the fact",
                        "properties": {
                          "original": {
                            "type": "string",
                            "description": "The original place string as entered",
                            "example": "Kirtland, Ohio, United States"
                          }
                        }
                      }
                    },
                    "required": [
                      "type"
                    ]
                  }
                }
              }
            }
          }
        },
        "required": [
          "persons"
        ]
      }
    }
  }
}
```